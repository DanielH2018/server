"""Containers with no readinessProbe are a decision, not an oversight.

A container with no readinessProbe AND no startupProbe is Ready the instant it starts, so a
Service publishes it before it can serve. That is worth fixing where a Service fronts it — and
actively harmful for a sidecar, because pod Ready is the AND of all containers: giving traefik's
CrowdSec agent a readinessProbe would take every route in the homelab out of service whenever
the agent hiccups. `traefik/templates/deployment.yaml.j2` says so in as many words.

The `startupProbe` half of that first sentence is load-bearing, and this guard used to omit it
(#1354). The kubelet holds a container's Ready condition at its zero value — false — for as long
as its startupProbe runs: `pkg/kubelet/prober/prober_manager.go`, `UpdatePodStatus`, which
`continue`s past the readiness lookup when `isContainerStarted` is false. A container with a
startupProbe and no readinessProbe therefore gates its pod's Ready condition anyway, which is the
exact outcome a sidecar exemption below exists to avoid. `_STARTUP_GATED` records the containers
where that gating is deliberate; any other exemption that grows a startupProbe is flagged.

So this guard does not demand universal coverage. It demands that every container without a
probe is listed below with the reason, which is the decision worth forcing on whoever adds the
next one.
"""

from _k8s_render import rendered_docs

_POD_KINDS = {"Deployment", "DaemonSet", "StatefulSet"}

# (role, container) -> why this container has no readinessProbe.
_NO_READINESS = {
    # ── sidecars: pod Ready is the AND of all containers, so a sidecar probe gates the
    #    whole pod's Service. Documented in each template.
    (
        "traefik",
        "crowdsec-agent",
    ): "sidecar readiness would gate the edge proxy's Service",
    (
        "traefik",
        "access-log-rotate",
    ): "sidecar; a tail/rotate hiccup must never gate the edge proxy's Service",
    ("authelia", "crowdsec-agent"): "same shape as traefik's sidecar; would gate SSO",
    (
        "uptime-kuma",
        "autokuma",
    ): "sidecar; readiness on /health would gate uptime-kuma's own Service until the first "
    "reconcile lands — a measured 76s Service-endpoint gap per pod replacement, which is why "
    "its startupProbe was removed for livenessProbe.initialDelaySeconds on 2026-09-06 (#1348)",
    # The exportarr metrics sidecars, same shape as the three above and one degree worse: a
    # readinessProbe here would let an EXPORTER outage pull its *arr out of its own Service,
    # so monitoring would take down the thing it monitors. They keep a livenessProbe, and a
    # dead exporter surfaces as `up == 0` on the `exportarr` scrape job.
    ("sonarr", "exportarr"): "metrics sidecar; readiness would gate sonarr's Service",
    ("radarr", "exportarr"): "metrics sidecar; readiness would gate radarr's Service",
    (
        "prowlarr",
        "exportarr",
    ): "metrics sidecar; readiness would gate prowlarr's Service",
    # ── game servers: a startupProbe already gates Service publication, so a readinessProbe
    #    would add nothing except a second probe hitting the same signal.
    (
        "terraria",
        "terraria",
    ): "startupProbe (grep /proc/net/tcp) already gates the Service; "
    "a tcpSocket connect probe would additionally log ~2900 fake 'is connecting...' "
    "joins/day in the game console",
    ("valheim", "valheim"): "startupProbe (grep /proc/net/udp+udp6) already gates the "
    "Service; UDP has no LISTEN state so tcpSocket is impossible anyway, and a connect "
    "probe would log a join attempt every cycle",
    # ── no Service fronts these; a livenessProbe restarts them and Kuma or Prometheus
    #    notices if they stop working. Readiness would add rollout gating only.
    ("autofix-bridge", "autofix-bridge"): "no Service; liveness plus Kuma monitors",
    ("janitorr", "janitorr"): "no Service; liveness plus Kuma monitors",
    ("karakeep", "time-tagger"): "no Service; liveness probe covers a hung worker",
    (
        "monitor-bridge",
        "monitor-bridge",
    ): "no Service; it is itself the monitor, and Kuma watches it",
    (
        "n8n",
        "n8n-runners",
    ): "no Service; task runners are dialled by the broker, not routed",
    ("crowdsec", "crowdsec-agent"): "DaemonSet agent, no Service; liveness covers it",
    # ── periodic workers with no server to probe. A stale Kuma push heartbeat catches
    #    'running but not doing its job', which readiness cannot see.
    ("cloudflare-ddns", "cloudflare-ddns"): "Kuma push heartbeat; no server to probe",
    ("scrutiny", "collector"): "periodic SMART collection; covered by a Kuma monitor",
    (
        "dri-device-plugin",
        "generic-device-plugin",
    ): "registers a kubelet socket; covered by monitor-bridge's check",
}


# Of the exemptions above, the ones that ALSO declare a startupProbe — so their pod is held
# NotReady while it runs, and the exemption's reason has to say that rather than claim the
# container never gates its Service. Both entries here say so in as many words.
_STARTUP_GATED = frozenset({("terraria", "terraria"), ("valheim", "valheim")})


def startup_gating_gaps(
    exempt_with_startup: set[tuple[str, str]],
    startup_gated: frozenset[tuple[str, str]],
) -> list[str]:
    """Problems in the `_NO_READINESS` x startupProbe overlap.

    `exempt_with_startup` is every recorded-exempt container that declares a startupProbe. An
    unrecorded one gates its Service despite being exempted on the grounds that it does not; a
    recorded one that lost its probe leaves the record describing a mechanism that is gone.
    """
    problems = []
    for role, name in sorted(exempt_with_startup - startup_gated):
        problems.append(
            f"{role}/{name} is exempt from readiness but declares a startupProbe, so it holds "
            "its pod NotReady while that probe runs. Either the exemption's reason is wrong, "
            "or the gating is deliberate — add it to _STARTUP_GATED"
        )
    for role, name in sorted(startup_gated - exempt_with_startup):
        problems.append(
            f"{role}/{name} is in _STARTUP_GATED but is no longer an exempt container with a "
            "startupProbe — remove it"
        )
    return problems


def _containers():
    for role, _tpl, doc in rendered_docs():
        if doc.get("kind") not in _POD_KINDS:
            continue
        spec = doc["spec"]["template"]["spec"]
        for container in spec.get("containers", []) or []:
            yield role, doc["metadata"]["name"], container


def test_every_container_without_readiness_is_recorded():
    offenders = []
    for role, workload, container in _containers():
        if "readinessProbe" in container:
            continue
        if (role, container["name"]) not in _NO_READINESS:
            offenders.append(f"{role}/{workload} container {container['name']}")

    assert not offenders, (
        "these containers have no readinessProbe, so a Service publishes them the instant they "
        "start.\nAdd a probe, or record the reason in _NO_READINESS in this file:\n  "
        + "\n  ".join(sorted(offenders))
    )


def test_the_record_has_no_stale_entries():
    """A container that gained a probe must leave the list, or the list stops meaning anything."""
    without = {
        (role, c["name"]) for role, _w, c in _containers() if "readinessProbe" not in c
    }
    live = {(role, c["name"]) for role, _w, c in _containers()}
    stale = sorted(k for k in _NO_READINESS if k in live and k not in without)
    gone = sorted(k for k in _NO_READINESS if k not in live)

    assert not stale, f"now has a readinessProbe — remove from _NO_READINESS: {stale}"
    assert not gone, f"no such container — remove from _NO_READINESS: {gone}"


def test_every_reason_is_substantive():
    thin = sorted(k for k, v in _NO_READINESS.items() if len(v.strip()) < 20)
    assert not thin, f"reason too thin to be a decision: {thin}"


def _exempt_with_startup() -> set[tuple[str, str]]:
    return {
        (role, c["name"])
        for role, _w, c in _containers()
        if "readinessProbe" not in c
        and "startupProbe" in c
        and (role, c["name"]) in _NO_READINESS
    }


def test_no_exemption_gates_its_service_with_an_unrecorded_startupProbe():
    census = _exempt_with_startup()
    # Non-vacuity: the census filters the rendered tree, and returns an empty set the moment
    # those two workloads are renamed — after which the assertion below checks nothing.
    assert _STARTUP_GATED <= census, (
        f"census lost a known startup-gated container: {sorted(_STARTUP_GATED - census)}"
    )
    problems = startup_gating_gaps(census, _STARTUP_GATED)
    assert not problems, "\n  ".join(["startupProbe gating is unrecorded:", *problems])


def test_an_unrecorded_startup_gated_exemption_is_flagged():
    """The red half: (uptime-kuma, autokuma) was exactly this shape until #1348."""
    problems = startup_gating_gaps({("uptime-kuma", "autokuma")}, _STARTUP_GATED)
    assert any("uptime-kuma/autokuma" in p for p in problems), problems


def test_a_recorded_startup_gated_exemption_is_clean():
    assert startup_gating_gaps(set(_STARTUP_GATED), _STARTUP_GATED) == []


def test_a_startup_gated_entry_that_lost_its_probe_is_flagged():
    """The stale arm's own red half, mirroring `test_the_record_has_no_stale_entries`.

    Without it the `startup_gated - exempt_with_startup` loop is driven by no input in this
    module and is only ever observed passing — the shape the repo has paid for twice.
    """
    problems = startup_gating_gaps(set(), _STARTUP_GATED)
    assert len(problems) == len(_STARTUP_GATED), problems
    assert all("remove it" in p for p in problems), problems
