"""A kubelet probe must not GET a large metrics body on a short period.

THE FAILURE THIS CLOSES (2026-08-29). node-exporter's liveness and readiness probes both used
`httpGet: /metrics`. kubelet's HTTP prober reads a bounded prefix of the response and closes
the connection; node_exporter's /metrics is 250,165 bytes over 312 families, so every probe was
cut off mid-body and the exporter logged `error encoding and sending metric family: ...
connection reset by peer` once per family it could no longer write — about 180 lines per probe.
At 8 probes a minute that was 21-24 lines/sec/pod and 97% of all k8s-namespace Loki ingest.

It is worth a guard rather than a comment because every signal around it read healthy. The pods
were Ready, the probes passed, Prometheus scraped fine, and the flood looked like a throttling
symptom (node-exporter was CFS-throttled on 42% of periods). Raising the CPU limit took
throttling to zero and changed the line rate not at all — the prober closes after its fixed
read however fast the body is produced. Nothing in the manifest looked wrong, which is why a
future role can reintroduce this without anyone noticing until Loki fills.

The rule: a probe that asks for a whole exporter's metrics may exist — it is the only thing
that detects a WEDGED collector, since a hung collector still answers `/` — but it must run
rarely enough that the resulting log volume is negligible. Frequent probes belong on a cheap
path.

Scope is derived from the rendered manifests, not a hardcoded role list, so a NEW workload that
adds a fast /metrics probe fails here rather than inheriting node-exporter's lesson by luck.
That is deliberate: this repo's most-repeated review finding is a guard whose corpus was
narrowed to the one case its fix touched.
"""

from __future__ import annotations

import pytest

from _k8s_render import rendered_docs

# Below this, a /metrics probe is cheap in the only sense that matters here: how often it makes
# an exporter serialise its whole registry into a socket the prober will close. 300s is what
# node-exporter's liveness probe uses, chosen so the residual is ~36 lines/min against the
# 1,440 lines/min the 30s version produced.
_MIN_PERIOD_S = 300

# The probe kinds kubelet drives on a timer. `startupProbe` is included: it runs at its own
# period until it first succeeds, so a fast one against /metrics floods exactly the same way
# during every rollout.
_PROBE_KEYS = ("livenessProbe", "readinessProbe", "startupProbe")

# Paths that serialise a full Prometheus registry. A query string is NOT enough to make one
# cheap — `?collect[]=<collector>` still drags the go_*/process_*/promhttp_* floor along, ~10KB,
# which sits within a couple of hundred bytes of the prober's read limit. Treat any path whose
# route is the metrics endpoint as expensive regardless of its parameters.
_METRICS_ROUTES = ("/metrics",)


def _is_metrics_route(path: str) -> bool:
    return path.split("?", 1)[0].rstrip("/") in [r.rstrip("/") for r in _METRICS_ROUTES]


def _probes():
    """(role, template, workload, container, probe kind, path, periodSeconds) for httpGet probes.

    periodSeconds is reported as kubelet's own default (10) when unset, because that is what
    actually runs — reading an absent field as "no period" would let the worst case through.
    """
    for role, tpl, doc in rendered_docs():
        spec = (doc.get("spec") or {}).get("template", {}).get("spec")
        if not isinstance(spec, dict):
            continue
        name = (doc.get("metadata") or {}).get("name", "?")
        for container in (spec.get("containers") or []) + (
            spec.get("initContainers") or []
        ):
            for key in _PROBE_KEYS:
                probe = container.get(key)
                if not isinstance(probe, dict) or "httpGet" not in probe:
                    continue
                path = str(probe["httpGet"].get("path", "/"))
                yield (
                    role,
                    tpl,
                    name,
                    container.get("name", "?"),
                    key,
                    path,
                    int(probe.get("periodSeconds", 10)),
                )


def test_no_frequent_probe_asks_for_a_full_metrics_body():
    """The rule itself, over every rendered workload."""
    offenders = [
        f"{role}/{tpl} {workload}/{cname} {kind} -> {path} every {period}s"
        for role, tpl, workload, cname, kind, path, period in _probes()
        if _is_metrics_route(path) and period < _MIN_PERIOD_S
    ]
    assert not offenders, (
        "a kubelet probe GETs a full metrics body more often than every "
        f"{_MIN_PERIOD_S}s — the prober closes mid-response and the exporter logs one line per "
        "unwritten metric family. Point it at a cheap path, or slow it down:\n  "
        + "\n  ".join(offenders)
    )


def test_the_rule_rejects_the_shape_it_was_written_for():
    """The reject half: the exact manifest that caused the flood must fail.

    Without this the check above passes trivially the day `_is_metrics_route` stops matching, or
    `_probes()` stops finding probes at all — the two ways a guard like this goes quietly
    vacuous. The fixture is node-exporter's readiness probe as it actually shipped.
    """
    role, tpl, workload, cname, kind, path, period = (
        "node-exporter",
        "daemonset.yaml.j2",
        "node-exporter",
        "node-exporter",
        "readinessProbe",
        "/metrics",
        10,
    )
    assert _is_metrics_route(path)
    assert period < _MIN_PERIOD_S, (
        f"{role}/{tpl} {workload}/{cname} {kind} -> {path} every {period}s"
    )


@pytest.mark.parametrize(
    "path",
    ["/metrics", "/metrics/", "/metrics?collect[]=uname", "/metrics?collect[]=loadavg"],
)
def test_filtering_a_metrics_path_does_not_make_it_cheap(path):
    """A query string must not buy an exemption.

    `?collect[]=uname` measures 10,116 bytes against 250,165 for the whole registry, which looks
    like a fix and is not: the floor is the exporter's own go_*/process_*/promhttp_* families,
    which ride along on every filtered response and land it within ~200 bytes of the prober's
    read limit. A node_exporter image bump moves that floor and the flood returns with nothing
    to show it changed.
    """
    assert _is_metrics_route(path)


@pytest.mark.parametrize("path", ["/", "/-/healthy", "/healthz", "/api/health"])
def test_cheap_paths_are_allowed_at_any_period(path):
    """The accept half — a small body may be probed as often as the workload needs."""
    assert not _is_metrics_route(path)


def test_the_corpus_is_not_empty():
    """A rendered-manifest guard that finds no probes asserts nothing.

    `_probes()` walks a nested shape (workload -> template -> containers -> probe -> httpGet); a
    change to any level could yield nothing while every assertion above still passes.
    """
    found = list(_probes())
    assert len(found) > 10, (
        f"only {len(found)} httpGet probes found across the rendered tree"
    )


def test_node_exporter_still_has_a_probe_that_renders_metrics():
    """The wedged-collector case must stay covered.

    The cheap fix for the flood is to point every probe at `/`, and that would pass every
    assertion above while removing the only thing that restarts a node-exporter whose collector
    has hung — a hung collector still answers `/`, so nothing else notices. This pins the half
    of the fix that is easy to lose: slowing the metrics probe down is correct, deleting it is
    not.
    """
    metrics_probes = [
        (kind, period)
        for role, _tpl, _workload, _cname, kind, path, period in _probes()
        if role == "node-exporter" and _is_metrics_route(path)
    ]
    assert metrics_probes, (
        "node-exporter has no probe left that asks for metrics — a wedged collector would keep "
        "answering / and never be restarted"
    )
    assert all(period >= _MIN_PERIOD_S for _kind, period in metrics_probes)
