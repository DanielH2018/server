"""Jellyfin's probes must set `timeoutSeconds` rather than inherit the 1s default.

Kubernetes defaults `timeoutSeconds` to 1, so a `/health` slower than one second counts as a
probe failure. On jellyfin that failed into two different symptoms on 2026-09-09 (#1500): the
readiness probe emptied the endpoint list, which made Traefik drop the `jellyfin` router and
return 404 for every request to the hostname, and the liveness probe killed the container 14
times with exit 137.

The guard is scoped to jellyfin on purpose. Only 8 of 60 k8s roles set `timeoutSeconds` at all,
so a repo-wide census would flag 52 roles that have never had a probe-timeout incident — a
different change, with a different argument behind it.

`_EXPECTED` is the non-vacuity half: the render must actually produce both probes. Without it a
template rename or a change to what `_k8s_render` collects would leave the loop iterating over
nothing, and an assertion over an empty set passes while checking nothing.
"""

from _k8s_render import rendered_docs

_POD_KINDS = {"Deployment", "DaemonSet", "StatefulSet"}

# (container, probe) pairs the render MUST contain. A missing member names itself in the diff.
_EXPECTED = {("jellyfin", "readinessProbe"), ("jellyfin", "livenessProbe")}


def _jellyfin_probes():
    """(container name, probe key, probe dict) for every probe jellyfin's manifests declare."""
    for role, _tpl, doc in rendered_docs():
        if role != "jellyfin" or doc.get("kind") not in _POD_KINDS:
            continue
        spec = doc["spec"]["template"]["spec"]
        for container in spec.get("containers", []) + spec.get("initContainers", []):
            for key in ("readinessProbe", "livenessProbe", "startupProbe"):
                if key in container:
                    yield container["name"], key, container[key]


def test_jellyfin_probes_set_an_explicit_timeout():
    probes = list(_jellyfin_probes())
    found = {(name, key) for name, key, _ in probes}
    missing = _EXPECTED - found
    assert not missing, (
        f"jellyfin's render declares no {sorted(missing)} — the guard is vacuous"
    )

    defaulted = [
        f"{name}.{key}" for name, key, probe in probes if "timeoutSeconds" not in probe
    ]
    assert not defaulted, (
        "these jellyfin probes inherit the 1s default timeout: "
        f"{sorted(defaulted)} — set timeoutSeconds explicitly (#1500)"
    )
