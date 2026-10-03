"""One-predicate properties of the rendered k8s manifests, one table row each.

Every row runs over `_k8s_render.rendered_docs()`, the deploy's own render, and `_property_table`
supplies the floor, the `subject gone` failure, the allowlist and the red/green proof for each.
A property with one selector and one predicate goes here as a row rather than in its own file.
One that needs a second render context, a cross-document join or a long exemption list keeps a
file (`test_container_security_context.py` is the example).

A row's `reason` is the long form a reader needs before changing the property. Keep it as
specific as the file docstring it replaced.

Run: uv run pytest ansible/tests/k8s/test_rendered_properties.py
"""

from dataclasses import replace

import pytest

from _helpers import REPO, manifests_rollout_timeout_s
from _k8s_render import pod_spec
from _property_table import Property, check

_K8S_ROLES = REPO / "ansible/roles/k8s"

_POD_KINDS = frozenset({"Deployment", "DaemonSet", "StatefulSet", "CronJob", "Job"})


def _deployments(role: str, tpl: str, doc: dict):
    if doc.get("kind") == "Deployment":
        yield doc["metadata"]["name"], doc.get("spec", {})


REVISION_HISTORY_LIMIT = 3


def _revision_history_offence(role: str, spec: dict) -> str | None:
    value = spec.get("revisionHistoryLimit")
    if value == REVISION_HISTORY_LIMIT:
        return None
    if value is None:
        return "no revisionHistoryLimit — keeps 10 dead ReplicaSets instead of 3"
    return f"revisionHistoryLimit is {value!r}, not {REVISION_HISTORY_LIMIT}"


_DEFAULT_DEADLINE_S = 600


def _deadline_offence(role: str, spec: dict) -> str | None:
    budget_s = manifests_rollout_timeout_s(_K8S_ROLES / role)
    deadline = spec.get("progressDeadlineSeconds", _DEFAULT_DEADLINE_S)
    if deadline >= budget_s:
        return None
    return (
        f"progressDeadlineSeconds {deadline} is below the {budget_s}s rollout budget, so "
        "`rollout status` fails on ProgressDeadlineExceeded before its own timeout"
    )


OBSERVABILITY = "observability"


def _observability_workloads(role: str, tpl: str, doc: dict):
    if (
        doc.get("kind") in _POD_KINDS
        and doc.get("metadata", {}).get("namespace") == OBSERVABILITY
    ):
        yield f"{doc['kind']}/{doc['metadata']['name']}", doc


def _tenancy_offence(role: str, doc: dict) -> str | None:
    if role == OBSERVABILITY:
        return None
    return f"role {role!r} renders a pod into the {OBSERVABILITY!r} namespace"


_PROBE_KEYS = ("readinessProbe", "livenessProbe", "startupProbe")


def _jellyfin_probes(role: str, tpl: str, doc: dict):
    if role != "jellyfin" or doc.get("kind") not in {
        "Deployment",
        "DaemonSet",
        "StatefulSet",
    }:
        return
    spec = pod_spec(doc)
    for container in spec.get("containers", []) + spec.get("initContainers", []):
        for key in _PROBE_KEYS:
            if key in container:
                yield f"{container['name']}.{key}", container[key]


def _probe_timeout_offence(role: str, probe: dict) -> str | None:
    if "timeoutSeconds" in probe:
        return None
    return "inherits the 1s default timeout — set timeoutSeconds explicitly (#1500)"


CONFIGARR_CACHE_VOLUME = "configarr-repos"


def _configarr_cache(role: str, tpl: str, doc: dict):
    if role != "configarr" or doc.get("kind") != "CronJob":
        return
    for volume in pod_spec(doc).get("volumes", []):
        if volume.get("name") == CONFIGARR_CACHE_VOLUME:
            yield CONFIGARR_CACHE_VOLUME, volume


def _ephemeral_offence(role: str, volume: dict) -> str | None:
    if "emptyDir" in volume and "persistentVolumeClaim" not in volume:
        return None
    return f"the clone cache is not an emptyDir: {volume}"


PROPERTIES = (
    Property(
        name="deployment-revision-history-limit",
        reason=(
            "Kubernetes defaults revisionHistoryLimit to 10, so an unpinned Deployment keeps ten "
            "scaled-to-zero ReplicaSets and `kubectl get rs -A` stops being readable. Rollbacks "
            "go through `git revert` + a redeploy, so depth is not a reason to raise it. "
            "`spec_shell` in ansible/templates/workload-shell.yml.j2 emits the pin and "
            "test_workload_shell_uses_the_macros.py refuses a hand-written one; this row reads "
            "the RENDERED spec, which tells a template that never calls the macro from one that "
            "does. A DaemonSet owns no ReplicaSets and is out of scope."
        ),
        select=_deployments,
        offence=_revision_history_offence,
        red=("nut", {"revisionHistoryLimit": 10}),
        green=("nut", {"revisionHistoryLimit": REVISION_HISTORY_LIMIT}),
        # 62 Deployments render. Close enough to notice a contraction.
        min_matches=55,
    ),
    Property(
        name="progress-deadline-covers-rollout-budget",
        reason=(
            "k8s/manifests/tasks/drain.yml runs `kubectl rollout status --timeout=<budget>` "
            "with the role's manifests_rollout_timeout, and `rollout status` also exits as soon "
            "as the Deployment is marked ProgressDeadlineExceeded (progressDeadlineSeconds, "
            "default 600). A pod stuck Pulling makes no progress, so a budget above the deadline "
            "is unreachable: prowlarr's 780s budget for a flaresolverr cold pull holds only "
            "because its templates raise the deadline to match."
        ),
        select=_deployments,
        offence=_deadline_offence,
        # prowlarr's budget is 780s, so the 600s default deadline cannot cover it.
        red=("prowlarr", {}),
        green=("prowlarr", {"progressDeadlineSeconds": 780}),
        min_matches=55,
        # The Deployments whose budget exceeds the default deadline: they carry the whole
        # census, since every role at the default budget passes on the default deadline.
        # Re-derive the set if `_DEFAULT_DEADLINE_S` or a role's budget moves.
        must_find=frozenset(
            {"prowlarr", "flaresolverr", "valheim", "sonarr", "radarr"}
        ),
    ),
    Property(
        name="observability-namespace-is-sole-tenant",
        reason=(
            "networkpolicy-observability.yaml.j2 admits a bare `podSelector: {}` as the "
            "intra-namespace ingress peer, which is sound only while observability is the one "
            "role rendering workloads into that namespace. A second role landing a pod there "
            "gets unrestricted ingress to every fenced pod in it with no policy change of its "
            "own. Give the new workload its own namespace, or replace the bare selector with an "
            "explicit per-workload peer list. docs/networkpolicy-default-deny.md, 'Slice 3 "
            "specifics', has the long form."
        ),
        select=_observability_workloads,
        offence=_tenancy_offence,
        red=("nut", {}),
        green=(OBSERVABILITY, {}),
        must_find=frozenset({"Deployment/prometheus", "Deployment/grafana"}),
    ),
    Property(
        name="jellyfin-probes-set-a-timeout",
        reason=(
            "Kubernetes defaults timeoutSeconds to 1, so a /health slower than a second is a "
            "probe failure. On jellyfin the readiness probe then empties the endpoints and "
            "Traefik answers 404 for the hostname, and the liveness probe kills the container "
            "with exit 137. Scoped to jellyfin on purpose: most roles set no timeout and have "
            "had no incident, which is a different change with a different argument."
        ),
        select=_jellyfin_probes,
        offence=_probe_timeout_offence,
        red=("jellyfin", {"httpGet": {"path": "/health", "port": 8096}}),
        green=(
            "jellyfin",
            {"httpGet": {"path": "/health", "port": 8096}, "timeoutSeconds": 5},
        ),
        must_find=frozenset({"jellyfin.readinessProbe", "jellyfin.livenessProbe"}),
    ),
    Property(
        name="configarr-repos-cache-is-ephemeral",
        reason=(
            "configarr's TRaSH/recyclarr clone cache is two public repos re-cloned in seconds. "
            "On a PVC it outlives the pod, and a clone that stopped updating (a moved default "
            "branch, a rebased history the fetch refuses) keeps serving old guides while the "
            "sync reports green. An emptyDir makes every nightly run start from upstream. "
            "test_cronjob_only_roles_include_the_gate.py covers the job's gate."
        ),
        select=_configarr_cache,
        offence=_ephemeral_offence,
        red=(
            "configarr",
            {
                "name": CONFIGARR_CACHE_VOLUME,
                "persistentVolumeClaim": {"claimName": "x"},
            },
        ),
        green=("configarr", {"name": CONFIGARR_CACHE_VOLUME, "emptyDir": {}}),
        must_find=frozenset({CONFIGARR_CACHE_VOLUME}),
    ),
)

_IDS = [p.name for p in PROPERTIES]


@pytest.mark.parametrize("prop", PROPERTIES, ids=_IDS)
def test_property_holds_on_the_render(prop: Property):
    problems = check(prop)
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("prop", PROPERTIES, ids=_IDS)
def test_property_red_fixture_is_flagged(prop: Property):
    assert prop.offence(*prop.red) is not None


@pytest.mark.parametrize("prop", PROPERTIES, ids=_IDS)
def test_property_green_fixture_is_clean(prop: Property):
    assert prop.offence(*prop.green) is None


def test_property_names_are_unique():
    assert len(set(_IDS)) == len(_IDS)


def _toy(**overrides) -> Property:
    """A row selecting ConfigMaps whose `data.ok` must be "yes"."""

    def select(role, tpl, doc):
        if doc.get("kind") == "ConfigMap":
            yield doc["metadata"]["name"], doc

    def offence(role, doc):
        return None if doc["data"]["ok"] == "yes" else "not ok"

    toy = Property(
        name="toy",
        reason="toy",
        select=select,
        offence=offence,
        red=("r", {"data": {"ok": "no"}}),
        green=("r", {"data": {"ok": "yes"}}),
    )
    return replace(toy, **overrides)


def _cm(name: str, ok: str) -> tuple[str, str, dict]:
    return (
        "r",
        "t.yaml.j2",
        {"kind": "ConfigMap", "metadata": {"name": name}, "data": {"ok": ok}},
    )


def test_check_is_clean_when_every_match_holds():
    assert (
        check(_toy(must_find=frozenset({"a"})), [_cm("a", "yes"), _cm("b", "yes")])
        == []
    )


def test_check_is_flagged_when_a_match_offends():
    problems = check(_toy(), [_cm("a", "yes"), _cm("b", "no")])
    assert any("[b]: not ok" in p for p in problems), problems


def test_check_is_flagged_when_the_subject_is_gone():
    problems = check(
        _toy(), [("r", "t.yaml.j2", {"kind": "Secret", "metadata": {"name": "a"}})]
    )
    assert problems == [
        "toy: subject gone: delete this row (the selector matched nothing)"
    ]


def test_check_is_flagged_below_the_floor_or_missing_a_named_member():
    problems = check(_toy(min_matches=2, must_find=frozenset({"z"})), [_cm("a", "yes")])
    assert any("floor is 2" in p for p in problems), problems
    assert any("never matched ['z']" in p for p in problems), problems


def test_check_is_clean_on_an_allowed_offender_and_flags_a_stale_allow_entry():
    allowed = _toy(allow={"b": "known exception"})
    assert check(allowed, [_cm("a", "yes"), _cm("b", "no")]) == []
    problems = check(allowed, [_cm("a", "yes"), _cm("b", "yes")])
    assert any("allow entry 'b' is stale" in p for p in problems), problems
