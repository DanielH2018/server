"""Guards on how this cluster bounds container resources, at both levels it works at.

Two levels, because either one alone leaves a hole:

  * **Per container, in the manifests.** Every container this repo renders or vendors sets
    requests and limits, or carries a named exemption. Nothing else enforces it — the sibling
    guard, test_container_security_context.py, covers securityContext and stops there. An
    unenforced 100% is one merge away from 99%, and the container that loses its limits is
    invisible: it schedules, it runs, and it is the one that takes the node down.
  * **Per namespace, as a LimitRange.** That covers exactly what the guard above cannot — an
    object that never went through Ansible. The `ctx-probe` pod in homelab is the standing
    example: hand-created with no resources at all.

The LimitRange's own risk is that it grows teeth. `min`/`max` REJECT every pod outside the
band; a default only fills in what a container left out. That difference is what keeps this
from refusing the workloads already rendered at 2Gi and 4Gi, and it is the same reason there
is no ResourceQuota — a namespace CPU/memory quota makes requests and limits mandatory on
every pod in it, converting a missing value into a failed deploy. Both are pinned below,
because both are a one-line edit away and neither announces itself.

Defaults are not rejection-free either, which is why the ceiling is checked and not just its
absence: a container that states a request and no limit is given `default` as its limit, and
the API server refuses the pod if that limit lands below the request. See
k8s_default_limitrange in group_vars/all.yml for why the ceiling is set where it is.
"""

from lib.proc_testing import run

from lib import yaml_fast
from _k8s_render import pod_spec, rendered_build_job_text, rendered_docs
from _role_census import role_dirs
from _helpers import REPO as _REPO
from _helpers import ALL_VARS, load_yaml
from renderer.renderer_lib.bundle import homelab_namespace_files
from renderer.renderer_lib.cluster import vendored_files
from renderer.renderer_lib.namespace import HOMELAB_NAMESPACE
from validate.k8s_manifests import SKIP_ROLES, k8s_entries


_POD_KINDS = {"Deployment", "DaemonSet", "StatefulSet", "Job", "CronJob"}
_LIMITRANGE_NAME = "default-container-limits"


def _all_vars() -> dict:
    return load_yaml(ALL_VARS)


_RESOURCE_KEYS = ("requests.cpu", "requests.memory", "limits.cpu", "limits.memory")
_REQUEST_ONLY = frozenset({"requests.cpu", "limits.cpu", "limits.memory"})
_LONGHORN_UNSIZED = (frozenset(_RESOURCE_KEYS), "upstream's default, unsized (#8543)")
_METALLB_UNSIZED = (frozenset(_RESOURCE_KEYS), "upstream's default, unsized (#8542)")
# A vendored container that may go without some keys: its id -> (the keys it lacks, why).
# The set is exact. A key that gets set, or a container that disappears, fails the guard as
# a stale exemption, so an entry cannot outlive its fix.
_VENDORED_EXEMPT = {
    "longhorn DaemonSet/longhorn-manager containers/longhorn-manager": (
        _REQUEST_ONLY,
        "a memory request and no limit by decision (#1243); "
        "tests/longhorn/test_longhorn_control_plane_resources.py holds it",
    ),
    "longhorn Deployment/longhorn-global-manager containers/longhorn-global-manager": (
        _REQUEST_ONLY,
        "a memory request and no limit, the manager's rule (#5968); "
        "tests/longhorn/test_longhorn_control_plane_resources.py holds it",
    ),
    "longhorn DaemonSet/longhorn-manager containers/pre-pull-share-manager-image": _LONGHORN_UNSIZED,
    "longhorn Deployment/longhorn-driver-deployer initContainers/wait-longhorn-manager": _LONGHORN_UNSIZED,
    "longhorn Deployment/longhorn-driver-deployer containers/longhorn-driver-deployer": _LONGHORN_UNSIZED,
    "longhorn Deployment/longhorn-ui containers/longhorn-ui": _LONGHORN_UNSIZED,
    "metallb Deployment/controller containers/controller": _METALLB_UNSIZED,
    "metallb DaemonSet/speaker containers/speaker": _METALLB_UNSIZED,
}


def _has_directive(node) -> bool:
    """Whether a patch carries a `$patch`-style key, which the overlay below does not model."""
    if isinstance(node, dict):
        return any(str(k).startswith("$") or _has_directive(v) for k, v in node.items())
    return isinstance(node, list) and any(_has_directive(item) for item in node)


def _containers(role: str, doc: dict):
    """(role, container id, container) for each container in a workload doc."""
    pod = pod_spec(doc)
    # initContainers included deliberately: an init container with no limit can consume
    # the node just as thoroughly as the app container, and it runs before anything is
    # watching the workload.
    for key in ("initContainers", "containers"):
        for container in pod.get(key) or []:
            where = f"{doc['kind']}/{doc['metadata']['name']} {key}/{container['name']}"
            yield role, f"{role} {where}", container


def _target(doc: dict) -> tuple:
    return (doc["kind"], doc["metadata"]["name"], doc["metadata"].get("namespace"))


def _vendored_workloads():
    """(role, doc) for each workload a role vendors, with its patches' `resources` merged in.

    Kustomize's strategic merge, narrowed to what this guard reads: a patch's container
    matches the upstream container of the same name, and each `resources` section merges
    key by key. The doc is what Flux applies for every field read here. A patch naming a
    workload or a container upstream lacks fails, as `kustomize build` does.
    """
    entries = k8s_entries()
    for role_dir in role_dirs():
        role = role_dir.name
        if role in SKIP_ROLES or role not in entries:
            continue
        workloads, patches = {}, []
        for path, text in sorted(vendored_files(role).items()):
            for doc in yaml_fast.safe_load_all(text):
                if not isinstance(doc, dict) or doc.get("kind") not in _POD_KINDS:
                    continue
                if path.startswith("upstream/"):
                    workloads[_target(doc)] = doc
                    continue
                assert not _has_directive(doc), (
                    f"{role}/files/{path} uses a patch directive this overlay cannot model"
                )
                patches.append((path, doc))
        for path, patch in patches:
            target = workloads.get(_target(patch))
            assert target is not None, (
                f"{role}/files/{path} patches {_target(patch)}, which no upstream file declares"
            )
            _merge_resources(target, patch, f"{role}/files/{path}")
        for doc in workloads.values():
            yield role, doc


def _merge_resources(target: dict, patch: dict, where: str) -> None:
    """Merge each patch container's `resources` into `target`'s container of the same name."""
    for key in ("initContainers", "containers"):
        upstream = {c["name"]: c for c in pod_spec(target).get(key) or []}
        for change in pod_spec(patch).get(key) or []:
            assert change["name"] in upstream, (
                f"{where} patches {key}/{change['name']}, which {_target(patch)} lacks"
            )
            merged = upstream[change["name"]].get("resources") or {}
            for section, values in (change.get("resources") or {}).items():
                merged[section] = {**(merged.get(section) or {}), **values}
            upstream[change["name"]]["resources"] = merged


def _pod_specs():
    """(role, container id, container) for every rendered container."""
    for role, _, doc in rendered_docs():
        if isinstance(doc, dict) and doc.get("kind") in _POD_KINDS:
            yield from _containers(role, doc)


def _missing_keys(container: dict) -> frozenset[str]:
    # Per KEY, not per dict. Asserting the `limits` dict is merely truthy passes a container
    # that sets `memory` and omits `cpu`, which then inherits the LimitRange's `cpu: "2"`
    # default.
    resources = container.get("resources") or {}
    return frozenset(
        name
        for name in _RESOURCE_KEYS
        if not (resources.get(name.split(".")[0]) or {}).get(name.split(".")[1])
    )


def _homelab_ns_items() -> list[dict]:
    """The objects the shared kind `homelab-namespace` renders, its only writer (D5 step 30).

    Read from the rendered files rather than from `namespace.py`'s dicts, so a LimitRange the
    kind's `kustomization.yaml` leaves out, or `type: Container` lost on the way to the file,
    shows here.
    """
    files = homelab_namespace_files("600s")
    directory = f"cluster/{HOMELAB_NAMESPACE}"
    listed = yaml_fast.safe_load(files[f"{directory}/kustomization.yaml"])["resources"]
    return [yaml_fast.safe_load(files[f"{directory}/{name}"]) for name in listed]


def _limitranges() -> dict[str, dict]:
    """Every LimitRange this repo applies, keyed by namespace."""
    found = {
        doc["metadata"]["namespace"]: doc
        for _, _, doc in rendered_docs()
        if isinstance(doc, dict) and doc.get("kind") == "LimitRange"
    }
    for item in _homelab_ns_items():
        if item.get("kind") == "LimitRange":
            found[item["metadata"]["namespace"]] = item
    return found


def test_every_rendered_container_sets_requests_and_limits() -> None:
    containers = list(_pod_specs())
    # A floor, not the exact count: this guard's failure mode is silence. If the render helper
    # stops yielding workloads — a renamed kind, a template that drops out — every container
    # passes because there are none, and the result is indistinguishable from full coverage.
    assert len(containers) > 90, (
        f"only {len(containers)} containers seen; the walk is broken"
    )
    missing = [
        f"{where} ({', '.join(sorted(keys))})"
        for _, where, container in containers
        if (keys := _missing_keys(container))
    ]
    assert not missing, (
        "these containers set no resource requests or limits: "
        + ", ".join(missing)
        + ". The namespace LimitRange would give them a default, but that default is a guard "
        "rail sized for something unmanaged — it is not a sizing decision for a workload this "
        "repo owns."
    )


def test_every_vendored_container_sets_requests_and_limits_or_is_exempt() -> None:
    """The render walk above holds no vendored file, so a vendored controller is checked here.

    system-upgrade-controller ran BestEffort past the guard above until its patch (#8225),
    because a role's `files/upstream/` and `files/patches/` never reach `rendered_docs()`.
    Their namespaces carry no LimitRange either, so a container left at upstream's
    `resources: {}` runs BestEffort.
    """
    found = {
        where: _missing_keys(c)
        for role, doc in _vendored_workloads()
        for _, where, c in _containers(role, doc)
    }
    # Non-vacuity: a walk that stopped reaching a vendoring role passes on nothing.
    assert {where.split()[0] for where in found} >= {
        "longhorn",
        "metallb",
        "system-upgrade",
    }

    unexempt = {
        where: sorted(keys)
        for where, keys in found.items()
        if keys and where not in _VENDORED_EXEMPT
    }
    assert not unexempt, (
        f"these vendored containers lack resource keys: {unexempt}. Set them with a "
        "kustomize patch under the role's files/patches/, or exempt the container in "
        "_VENDORED_EXEMPT with its reason."
    )
    stale = {
        where: (sorted(keys), sorted(found.get(where, ())), why)
        for where, (keys, why) in _VENDORED_EXEMPT.items()
        if found.get(where) != keys
    }
    assert not stale, (
        "these exemptions no longer match the vendored container (exempt keys, keys "
        f"actually missing, reason): {stale}. Narrow or delete the entry."
    )


def test_a_patch_completes_upstream_resources_and_a_partial_patch_is_flagged() -> None:
    def workload(resources: dict) -> dict:
        container = {"name": "app", "resources": resources}
        return {
            "kind": "Deployment",
            "metadata": {"name": "x"},
            "spec": {"template": {"spec": {"containers": [container]}}},
        }

    def merged(patch_resources: dict) -> frozenset[str]:
        target = workload({"requests": {"memory": "64Mi"}})
        _merge_resources(target, workload(patch_resources), "fixture")
        return _missing_keys(pod_spec(target)["containers"][0])

    full = {"requests": {"cpu": "10m"}, "limits": {"cpu": "1", "memory": "1Gi"}}
    assert merged(full) == frozenset()
    assert merged({"limits": {"memory": "1Gi"}}) == {"requests.cpu", "limits.cpu"}


def test_the_roles_outside_the_render_walk_are_accounted_for() -> None:
    """rendered_docs() cannot see every pod that lands in these namespaces. Name the gap.

    The walk covers roles that are `containers_list` members. The caller-rendered role is
    not: `image-builder`'s build Job is a pod that lands
    in `homelab` all the same, which is exactly the population the LimitRange exists for. It is
    rendered here with a caller's variables instead.

    The dangerous shape is a container that states a request and no limit: it takes `default`
    as its limit and is refused if the request is higher. A build job asking for 4Gi would
    stop working, and it would surface hours later as a failed image build rather than here.
    """
    job = yaml_fast.safe_load(rendered_build_job_text())
    pod = job["spec"]["template"]["spec"]
    excluded = {
        f"image-builder/build-job.yaml.j2 {c['name']}": (
            bool((c.get("resources") or {}).get("requests")),
            bool((c.get("resources") or {}).get("limits")),
        )
        for c in pod.get("initContainers", []) + pod["containers"]
    }

    assert excluded, "the build Job renders no container; check the render"
    # Non-vacuity: if the Job dropped both, the both-or-neither check below holds on nothing.
    assert any(
        has_requests or has_limits for has_requests, has_limits in excluded.values()
    ), (
        "the build Job no longer renders a request or a limit on any container. If it "
        "genuinely dropped both, it takes the namespace defaults; drop this guard rather than "
        "leaving it passing on nothing."
    )
    for where, (has_requests, has_limits) in excluded.items():
        # Either both or neither. Both means the LimitRange never touches it; neither means it
        # takes the defaults, which is the control doing its job. Requests-only is the one
        # combination that can be refused at admission.
        assert has_requests == has_limits, (
            f"{where} sets a request without a limit. It will take the namespace default as "
            "its limit and be REFUSED if the request is higher. Set both, or neither."
        )


def test_both_workload_namespaces_get_a_limitrange() -> None:
    all_vars = _all_vars()
    expected = {all_vars["k8s_namespace"], all_vars["k8s_observability_namespace"]}
    found = _limitranges()
    assert set(found) == expected
    # One name across both, so the rollback line in group_vars deletes what it says it does.
    assert {doc["metadata"]["name"] for doc in found.values()} == {_LIMITRANGE_NAME}


def test_the_limitrange_sets_defaults_only() -> None:
    for namespace, doc in _limitranges().items():
        for entry in doc["spec"]["limits"]:
            assert entry["type"] == "Container", namespace
            assert entry.get("default"), (
                f"{namespace}: no default limit, so it bounds nothing"
            )
            assert entry.get("defaultRequest"), f"{namespace}: no default request"
            assert "min" not in entry, (
                f"{namespace}: a `min` REJECTS any pod below it at admission, where a default "
                "only fills one in. See k8s_default_limitrange in group_vars/all.yml."
            )
            assert "max" not in entry, (
                f"{namespace}: a `max` REJECTS any pod above it at admission. Several "
                "workloads here are already at 2Gi and 4Gi."
            )
            # The ceiling is a safety property, not a preference. A container that states a
            # request and no limit takes `default` as its limit, and the API server refuses
            # the pod when that limit is below the stated request — so lowering this turns
            # the guard rail into an admission failure for requests-only Helm charts.
            assert entry["default"]["memory"] == "2Gi", (
                f"{namespace}: the memory ceiling refuses any requests-only pod asking for "
                "more than it. Read the rejection path in group_vars/all.yml before lowering."
            )


def test_no_resourcequota_is_declared_anywhere() -> None:
    # Grepped rather than read off the rendered docs: a ResourceQuota added to a role that
    # renders no manifests, or staged by a `kubectl create quota`, would never appear in
    # rendered_docs() and this is the decision, not the mechanism. Matched on the two forms
    # that APPLY one, not on the bare word — the reasoning that rules it out names it
    # repeatedly, and a guard that trips on its own rationale gets deleted rather than read.
    scope = ["k8s/", "inventory/", "secrets/cluster-manifests/"]
    # A pathspec that names no tracked file makes the grep below pass over nothing.
    for path in scope:
        listed = run(["git", "ls-files", "--", path], cwd=_REPO, check=True).stdout
        assert listed.strip(), (
            f"{path} holds no tracked file; the grep below reads nothing"
        )
    hits = run(
        [
            "git",
            "grep",
            "-lE",
            r"kind: ResourceQuota|create (resource)?quota",
            "--",
            *scope,
        ],
        cwd=_REPO,
        check=False,
    ).stdout.split()
    assert hits == [], (
        f"ResourceQuota declared in {hits}. A namespace CPU/memory quota makes requests and "
        "limits MANDATORY on every pod in the namespace — a pod that omits them stops being "
        "defaulted and starts being refused. If this is deliberate, the reasoning in "
        "k8s_default_limitrange (group_vars/all.yml) is what needs updating first."
    )


def test_the_limitrange_values_live_in_group_vars_only() -> None:
    all_vars = _all_vars()
    assert set(all_vars["k8s_default_limitrange"]) == {"default", "defaultRequest"}

    # Two readers — the renderer's homelab-namespace kind for homelab, the observability role
    # for observability — so a role default would be invisible to one of them and silently win
    # for the other.
    duplicated = [
        str(path.relative_to(_REPO))
        for path in _REPO.glob("k8s/roles/**/defaults/main.yml")
        if "k8s_default_limitrange" in path.read_text()
    ]
    assert duplicated == [], f"k8s_default_limitrange redefined in {duplicated}"
