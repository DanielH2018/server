"""Guards on how this cluster bounds container resources, at both levels it works at.

Two levels, because either one alone leaves a hole:

  * **Per container, in the manifests.** Every container this repo renders sets requests and
    limits. That was true when measured (105 of 105) and nothing enforced it — the sibling
    guard, test_container_security_context.py, covers securityContext and stops there. An
    unenforced 100% is one merge away from 99%, and the container that loses its limits is
    invisible: it schedules, it runs, and it is the one that takes the node down.
  * **Per namespace, as a LimitRange.** That covers exactly what the guard above cannot — an
    object that never went through Ansible. The `ctx-probe` pod in homelab is the standing
    example, hand-created on 2026-08-14 with no resources at all.

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

from __future__ import annotations

import subprocess
from pathlib import Path

import yaml
from _k8s_render import rendered_docs
from ansible.plugins.filter.core import combine
from jinja2.nativetypes import NativeEnvironment

_REPO = Path(__file__).resolve().parents[2]
_DEPLOY = _REPO / "ansible/deploy.yml"
_ALL_VARS = _REPO / "ansible/inventory/group_vars/all.yml"

_POD_KINDS = {"Deployment", "DaemonSet", "StatefulSet", "Job", "CronJob"}
_LIMITRANGE_NAME = "default-container-limits"


def _all_vars() -> dict:
    return yaml.safe_load(_ALL_VARS.read_text())


def _pod_specs():
    """(role, template, container) for every container in every rendered manifest."""
    for role, name, doc in rendered_docs():
        if not isinstance(doc, dict) or doc.get("kind") not in _POD_KINDS:
            continue
        spec = doc["spec"]
        if doc["kind"] == "CronJob":
            spec = spec["jobTemplate"]["spec"]
        pod = spec["template"]["spec"]
        # initContainers included deliberately: an init container with no limit can consume
        # the node just as thoroughly as the app container, and it runs before anything is
        # watching the workload.
        for key in ("initContainers", "containers"):
            for container in pod.get(key) or []:
                yield role, name, f"{key}/{container['name']}", container


def _eval(node, ctx, env):
    """Expand `{{ ... }}` through a structure, keeping non-string results as objects.

    A NativeEnvironment rather than a plain one because the expressions under test PRODUCE
    structures — `{{ k8s_default_limitrange | combine(...) }}` is a dict, and a normal Jinja
    env would hand back its repr as a string, which then asserts clean against nothing.
    """
    if isinstance(node, str):
        return env.from_string(node).render(ctx) if "{{" in node else node
    if isinstance(node, dict):
        return {k: _eval(v, ctx, env) for k, v in node.items()}
    if isinstance(node, list):
        return [_eval(v, ctx, env) for v in node]
    return node


def _k8s_play() -> dict:
    for play in yaml.safe_load(_DEPLOY.read_text()) or []:
        if "k8s" in str(play.get("name", "")).lower():
            return play
    raise AssertionError("deploy.yml no longer has a k8s play")


def _homelab_ns_items() -> list[dict]:
    """The objects deploy.yml applies alongside the homelab namespace, fully expanded."""
    task = next(
        t
        for t in _k8s_play().get("pre_tasks", [])
        if "Build the workload namespace manifest" in str(t.get("name", ""))
    )
    env = NativeEnvironment()
    # The real filter, not a shim: `combine` decides whether `type: Container` reaches the
    # LimitRange at all, and that is the field this test is about.
    env.filters["combine"] = combine

    ctx = dict(_all_vars())
    ctx.update(_eval(task.get("vars", {}), ctx, env))
    manifest = _eval(
        task["ansible.builtin.set_fact"]["deploy_k8s_ns_manifest"], ctx, env
    )
    assert manifest["kind"] == "List", (
        "the namespace apply is no longer a multi-object List"
    )
    return list(manifest["items"])


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
        f"{role}/{name} {which}"
        for role, name, which, container in containers
        if not (container.get("resources") or {}).get("requests")
        or not (container.get("resources") or {}).get("limits")
    ]
    assert not missing, (
        "these containers set no resource requests or limits: "
        + ", ".join(missing)
        + ". The namespace LimitRange would give them a default, but that default is a guard "
        "rail sized for something unmanaged — it is not a sizing decision for a workload this "
        "repo owns."
    )


def test_the_roles_outside_the_render_walk_are_accounted_for() -> None:
    """rendered_docs() cannot see every pod that lands in these namespaces. Name the gap.

    The walk covers roles that are `containers_list` members. Four are not, and two of those
    ship pod templates — so the guard above says "every rendered container" while meaning
    "every container in a containers_list role". Their pods land in `homelab` all the same,
    which is exactly the population the LimitRange exists for.

    The dangerous shape is a container that states a request and no limit: it takes `default`
    as its limit and is refused if the request is higher. A build job asking for 4Gi would
    stop working, and it would surface hours later as a failed image build rather than here.
    """
    excluded = {}
    for role in ("image-builder", "seed-volume"):
        for tpl in sorted(
            (_REPO / "ansible/roles/k8s" / role / "templates").glob("*.j2")
        ):
            text = tpl.read_text()
            if "containers:" not in text:
                continue
            excluded[f"{role}/{tpl.name}"] = (
                "requests:" in text,
                "limits:" in text,
            )

    assert excluded, "neither role ships a pod template any more; drop this guard"
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


def test_the_namespace_is_applied_before_its_limitrange() -> None:
    kinds = [item["kind"] for item in _homelab_ns_items()]
    assert kinds.index("Namespace") < kinds.index("LimitRange"), (
        "a LimitRange is namespace-scoped and fails to apply before its namespace exists"
    )


def test_the_namespace_apply_reports_changed_per_line() -> None:
    task = next(
        t
        for t in _k8s_play().get("pre_tasks", [])
        if "Apply the workload namespace" in str(t.get("name", ""))
    )
    changed_when = str(task["changed_when"])
    # A whole-stdout substring test is the specific bug: with two objects applied together,
    # `namespace/homelab unchanged` on line one hides `limitrange/... created` on line two,
    # and the run that created the LimitRange is the one that reports ok.
    assert "stdout_lines" in changed_when, (
        "this task applies more than one object, so its changed test has to read stdout "
        f"line by line: {changed_when}"
    )
    assert "deploy_k8s_ns_apply.stdout " not in f"{changed_when} ", changed_when


def test_no_resourcequota_is_declared_anywhere() -> None:
    # Grepped rather than read off the rendered docs: a ResourceQuota added to a role that
    # renders no manifests, or staged by a `kubectl create quota`, would never appear in
    # rendered_docs() and this is the decision, not the mechanism. Matched on the two forms
    # that APPLY one, not on the bare word — the reasoning that rules it out names it
    # repeatedly, and a guard that trips on its own rationale gets deleted rather than read.
    hits = subprocess.run(
        [
            "git",
            "grep",
            "-lE",
            r"kind: ResourceQuota|create (resource)?quota",
            "--",
            "ansible/",
            # This file states the pattern, so it matches itself — which passed while the
            # file was untracked and failed on the commit that added it. Tests apply nothing
            # to the cluster, so excluding them costs no coverage.
            ":!ansible/tests/",
        ],
        cwd=_REPO,
        capture_output=True,
        text=True,
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

    # Two roles read it — deploy.yml for homelab, claude-otel for observability — so a role
    # default would be invisible to one of them and silently win for the other.
    duplicated = [
        str(path.relative_to(_REPO))
        for path in _REPO.glob("ansible/roles/**/defaults/main.yml")
        if "k8s_default_limitrange" in path.read_text()
    ]
    assert duplicated == [], f"k8s_default_limitrange redefined in {duplicated}"
