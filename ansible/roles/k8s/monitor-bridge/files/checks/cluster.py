"""Cluster checks for monitor-bridge.

Covers the cAdvisor trio (restarts, OOM, CPU throttle) and the Prometheus gates;
the Kubernetes workload arms are the k8s-workloads rules, and the Traefik edge is observability's `edge.yaml`.

Slice 6 of the check.py split. Reads config as `cfg.X`, every query through the `src` argument
(`bridge.sources.Sources`) and its streak counters as `src.state` (`bridge.streaks.State`), so a
test hands in a fake `src`; the verdicts it from-imports from verdicts.cluster are patched on THIS
module, where they are bound.
Rule and enforcement: bridge/config.py's header.
"""

from collections.abc import Callable

from bridge.config import Config
from bridge.sources import Sources
import bridge.net
import bridge.streaks
from verdicts.cluster import (
    cadvisor_coverage_shortfall,
)


def _top_offenders(
    vector: list[tuple[dict, float]], label: str, predicate: Callable[[float], bool]
) -> list[tuple[str, float]]:
    """Names (by `label`) of series matching predicate(value), sorted by value desc."""
    hits = [(m.get(label, "?"), v) for m, v in vector if predicate(v)]
    hits.sort(key=lambda nv: -nv[1])
    return hits


def _cadvisor_blind(
    cfg: Config, src: Sources, key: str, vec: list[tuple[dict, float]], what: str
) -> tuple[bool, str] | None:
    """(ok, msg) when `vec` covers too few pods for an offender filter to mean anything, else None.

    Called on the PRE-FILTER vector, which is why each check hands in the one it already fetched.
    Holds `up` (saying so) while the shortfall is younger than CADVISOR_CONSECUTIVE cycles, then
    fails. Any covered cycle resets that check's streak.
    """
    msg = cadvisor_coverage_shortfall(len(vec), cfg.CADVISOR_PODS_MIN, what)
    if msg is None:
        src.state.cadvisor_streaks[key] = 0
        return None
    src.state.cadvisor_streaks[key], ok, out = bridge.streaks.down_streak(
        src.state.cadvisor_streaks.get(key, 0),
        cfg.CADVISOR_CONSECUTIVE,
        msg,
        "kubelet restart grace",
        held_label="cAdvisor coverage shortfall",
    )
    return ok, out


def check_restarts(cfg: Config, src: Sources) -> tuple[bool, str]:
    """Containers restarting more than RESTART_MAX times within RESTART_WINDOW.

    Catches crash-loops that an intermittent up-check can miss.
    """
    vec = src.prom_vector(
        "sum by (pod) (changes(container_start_time_seconds%s[%s]))"
        % (
            bridge.net.cadvisor_sel('container!=""', 'container!="POD"'),
            cfg.RESTART_WINDOW,
        ),
    )
    blind = _cadvisor_blind(cfg, src, "restarts", vec, "restart loops")
    if blind is not None:
        return blind
    offenders = _top_offenders(vec, "pod", lambda v: v > cfg.RESTART_MAX)
    if offenders:
        desc = ", ".join("%s (%.0f)" % (n, v) for n, v in offenders[:5])
        return False, "%d container(s) restarting >%.0fx in %s: %s" % (
            len(offenders),
            cfg.RESTART_MAX,
            cfg.RESTART_WINDOW,
            desc,
        )
    return True, "no restart loops in %s" % cfg.RESTART_WINDOW


def check_oom(cfg: Config, src: Sources) -> tuple[bool, str]:
    """Containers OOM-killed within OOM_WINDOW, naming each one.

    Closes the loop on the per-container memory limits (deploy.resources). An empty vector used to
    read as green here, which is how OOM kills went unmonitored for the whole Phase G window;
    _cadvisor_blind now reports that as UNKNOWN.
    """
    vec = src.prom_vector(
        "sum(increase(container_oom_events_total%s[%s])) by (pod)"
        % (
            bridge.net.cadvisor_sel('container!=""', 'container!="POD"'),
            cfg.OOM_WINDOW,
        ),
    )
    blind = _cadvisor_blind(cfg, src, "oom", vec, "OOM kills")
    if blind is not None:
        return blind
    offenders = _top_offenders(vec, "pod", lambda v: v > 0)
    if offenders:
        desc = ", ".join("%s (%.0f)" % (n, v) for n, v in offenders[:5])
        return False, "%d container(s) OOM-killed in %s: %s" % (
            len(offenders),
            cfg.OOM_WINDOW,
            desc,
        )
    return True, "no OOM kills in %s" % cfg.OOM_WINDOW


def check_cpu_throttle(cfg: Config, src: Sources) -> tuple[bool, str]:
    """Containers under *sustained* CPU CFS throttling within CPU_WINDOW, naming each one.

    A container pinned at its `deploy.resources` cpu limit is throttled (slowed) without
    OOMing, restarting, or 5xx-ing — invisible to the other checks. We alert only when
    BOTH conditions hold, so noise doesn't page:

      1. throttled/total CFS *periods* > CPU_THROTTLE_PCT — the fraction of enforcement
         periods that hit the cap (the cue the resources() macro names for raising a cap);
      2. throttled *seconds* per second > CPU_MIN_THROTTLED_CORES — the absolute CPU time
         (in cores) actually lost to throttling.

    Condition 1 alone fires constantly for tiny low-limit utility containers that briefly
    burst over their per-period slice while losing negligible absolute time (e.g. a 0.1-cpu
    sidecar at 90% throttled periods but 0.0001 cores lost) — a perpetual false `down`. The
    cores floor — the same volume-floor idea as the edge rules' 0.05 rps floor — gates
    those out, so the monitor pushes `up` and only goes `down` on genuine starvation.
    Containers with no cpu limit give 0/0 -> NaN for condition 1 (NaN comparisons are False)
    and are ignored. Absent cfs metrics used to empty both queries and read green; the floor in
    _cadvisor_blind reports that as UNKNOWN instead. Its vector is the smallest of the three —
    only pods carrying a cpu limit — which is what set the shared floor.

    On top of the two gates, CPU_CONSECUTIVE adds hysteresis: only the Nth consecutive
    breaching cycle goes `down` (~(N×INTERVAL)s of continuous throttling at the loop
    cadence). One- or two-cycle bursts — flaresolverr solving a challenge, homepage
    briefly hugging the cores floor — push `up` with the offender named in the msg, so
    the evidence stays in the bridge log without paging. A clean cycle resets the streak.
    """
    sel = bridge.net.cadvisor_sel('container!=""', 'container!="POD"')
    ratio_vec = src.prom_vector(
        "sum(rate(container_cpu_cfs_throttled_periods_total%s[%s])) by (pod) "
        "/ sum(rate(container_cpu_cfs_periods_total%s[%s])) by (pod)"
        % (sel, cfg.CPU_WINDOW, sel, cfg.CPU_WINDOW),
    )
    blind = _cadvisor_blind(cfg, src, "cpu", ratio_vec, "CPU throttling")
    if blind is not None:
        src.state.cpu_breach_streak = 0
        return blind
    lost_cores = dict(
        (m.get("pod", "?"), v)
        for m, v in src.prom_vector(
            "sum(rate(container_cpu_cfs_throttled_seconds_total%s[%s])) by (pod)"
            % (sel, cfg.CPU_WINDOW),
        )
    )
    threshold = cfg.CPU_THROTTLE_PCT / 100.0
    offenders = []
    for m, ratio in ratio_vec:
        name = m.get("pod", "?")
        lost = lost_cores.get(name, 0.0)
        if ratio > threshold and lost > cfg.CPU_MIN_THROTTLED_CORES:
            offenders.append((name, ratio, lost))
    offenders.sort(key=lambda nrl: -nrl[1])
    if not offenders:
        src.state.cpu_breach_streak = 0
        return True, "no sustained CPU throttling in %s" % cfg.CPU_WINDOW
    src.state.cpu_breach_streak += 1
    desc = ", ".join(
        "%s (%.0f%%, %.2f cores)" % (n, r * 100, lc) for n, r, lc in offenders[:5]
    )
    if src.state.cpu_breach_streak < cfg.CPU_CONSECUTIVE:
        return True, "throttling streak %d/%d (not alerting yet): %s" % (
            src.state.cpu_breach_streak,
            cfg.CPU_CONSECUTIVE,
            desc,
        )
    return (
        False,
        "%d container(s) CPU-throttled >%.0f%% & >%.2f cores for %d cycles: %s"
        % (
            len(offenders),
            cfg.CPU_THROTTLE_PCT,
            cfg.CPU_MIN_THROTTLED_CORES,
            src.state.cpu_breach_streak,
            desc,
        ),
    )


def check_prometheus(cfg: Config, src: Sources) -> tuple[bool, str]:
    """Is Prometheus itself reachable and answering queries?

    A trivial `vector(1)` instant query returns 1.0 whenever Prometheus is up; if it's
    down/unreachable prom_scalar raises (connection error) and run_once renders this monitor
    `down` with the error. This is the single root-cause signal for the prom-dependent checks:
    run_once probes it FIRST each cycle and, when it's down, SUPPRESSES the metric checks (which
    would otherwise all fail at once — one outage, a storm of identical pages) so only this
    monitor alerts. A single scrape target being down (Prometheus up, one exporter gone) still
    surfaces separately as ScrapeTargetDown — a distinct condition from this one.
    """
    val = src.prom_scalar("vector(1)")
    if val is None:
        return False, "Prometheus answered but returned no data for vector(1)"
    return True, "Prometheus reachable"
