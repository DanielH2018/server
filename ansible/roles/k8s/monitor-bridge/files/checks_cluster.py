"""Cluster checks for monitor-bridge — the cAdvisor trio (restarts, OOM, CPU throttle), the
Prometheus gates, scrape targets, Traefik 5xx and latency, and k3s workload health.

Slice 6 of the check.py split. Reads config as `cfg.X`, the fetch layer as `bridge_io.X`,
the shared streak counter as `bridge_streaks.X` and the Loki arm as
`checks_logs.with_log_errors`, so the tests' patches on those modules reach it; the verdicts
it from-imports from verdicts_cluster are patched on THIS module, where they are bound.
`_cadvisor_streaks` and `_cpu_breach_streak` live here beside the code that mutates them.
Rule and enforcement: bridge_config.py's header.
"""

import bridge_config as cfg
import bridge_io
import bridge_streaks
import checks_logs
from verdicts_cluster import (
    cadvisor_coverage_shortfall,
    extended_resource_verdict,
    k8s_workloads_verdict,
    ksm_resource_label,
    targets_verdict,
)


# Keyed per check, exactly like _host_origin_streaks. NOT one shared counter: all three checks run
# in the same cycle, so a single counter would take three increments per cycle and blow through
# CADVISOR_CONSECUTIVE inside the first one — hysteresis that silently does nothing.
_cadvisor_streaks: dict[str, int] = {}


def _top_offenders(vector, label, predicate):
    """Names (by `label`) of series matching predicate(value), sorted by value desc."""
    hits = [(m.get(label, "?"), v) for m, v in vector if predicate(v)]
    hits.sort(key=lambda nv: -nv[1])
    return hits


def _cadvisor_blind(key, vec, what):
    """(ok, msg) when `vec` covers too few pods for an offender filter to mean anything, else None.

    Called on the PRE-FILTER vector, which is why each check hands in the one it already fetched.
    Holds `up` (saying so) while the shortfall is younger than CADVISOR_CONSECUTIVE cycles, then
    fails. Any covered cycle resets that check's streak.
    """
    msg = cadvisor_coverage_shortfall(len(vec), cfg.CADVISOR_PODS_MIN, what)
    if msg is None:
        _cadvisor_streaks[key] = 0
        return None
    _cadvisor_streaks[key], ok, out = bridge_streaks.down_streak(
        _cadvisor_streaks.get(key, 0),
        cfg.CADVISOR_CONSECUTIVE,
        msg,
        "kubelet restart grace",
        held_label="cAdvisor coverage shortfall",
    )
    return ok, out


def check_restarts():
    """Containers restarting more than RESTART_MAX times within RESTART_WINDOW.

    Catches crash-loops that an intermittent up-check can miss.
    """
    vec = bridge_io.prom_vector(
        "sum by (pod) (changes(container_start_time_seconds%s[%s]))"
        % (
            bridge_io.cadvisor_sel('container!=""', 'container!="POD"'),
            cfg.RESTART_WINDOW,
        )
    )
    blind = _cadvisor_blind("restarts", vec, "restart loops")
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


def check_oom():
    """Containers OOM-killed within OOM_WINDOW, naming each one.

    Closes the loop on the per-container memory limits (deploy.resources). An empty vector used to
    read as green here, which is how OOM kills went unmonitored for the whole Phase G window;
    _cadvisor_blind now reports that as UNKNOWN.
    """
    vec = bridge_io.prom_vector(
        "sum(increase(container_oom_events_total%s[%s])) by (pod)"
        % (bridge_io.cadvisor_sel('container!=""', 'container!="POD"'), cfg.OOM_WINDOW)
    )
    blind = _cadvisor_blind("oom", vec, "OOM kills")
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


# Kept as its own module int rather than folded into _down_streaks below: its down branch
# is bespoke (the page message embeds the throttle thresholds), unlike the other four
# checks, which all call the shared down_streak() helper. See down_streak()'s docstring.
_cpu_breach_streak = 0


def check_cpu_throttle():
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
    cores floor — the same volume-floor idea as check_traefik_5xx's TRAEFIK_MIN_RPS — gates
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
    global _cpu_breach_streak
    sel = bridge_io.cadvisor_sel('container!=""', 'container!="POD"')
    ratio_vec = bridge_io.prom_vector(
        "sum(rate(container_cpu_cfs_throttled_periods_total%s[%s])) by (pod) "
        "/ sum(rate(container_cpu_cfs_periods_total%s[%s])) by (pod)"
        % (sel, cfg.CPU_WINDOW, sel, cfg.CPU_WINDOW)
    )
    blind = _cadvisor_blind("cpu", ratio_vec, "CPU throttling")
    if blind is not None:
        _cpu_breach_streak = 0
        return blind
    lost_cores = dict(
        (m.get("pod", "?"), v)
        for m, v in bridge_io.prom_vector(
            "sum(rate(container_cpu_cfs_throttled_seconds_total%s[%s])) by (pod)"
            % (sel, cfg.CPU_WINDOW)
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
        _cpu_breach_streak = 0
        return True, "no sustained CPU throttling in %s" % cfg.CPU_WINDOW
    _cpu_breach_streak += 1
    desc = ", ".join(
        "%s (%.0f%%, %.2f cores)" % (n, r * 100, lc) for n, r, lc in offenders[:5]
    )
    if _cpu_breach_streak < cfg.CPU_CONSECUTIVE:
        return True, "throttling streak %d/%d (not alerting yet): %s" % (
            _cpu_breach_streak,
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
            _cpu_breach_streak,
            desc,
        ),
    )


def check_prometheus():
    """Is Prometheus itself reachable and answering queries?

    A trivial `vector(1)` instant query returns 1.0 whenever Prometheus is up; if it's
    down/unreachable prom_scalar raises (connection error) and run_once renders this monitor
    `down` with the error. This is the single root-cause signal for the prom-dependent checks:
    run_once probes it FIRST each cycle and, when it's down, SUPPRESSES the metric checks (which
    would otherwise all fail at once — one outage, a storm of identical pages) so only this
    monitor alerts. A single scrape target being down (Prometheus up, one exporter gone) still
    surfaces separately on the Scrape Targets monitor — a distinct condition from this one.
    """
    val = bridge_io.prom_scalar("vector(1)")
    if val is None:
        return False, "Prometheus answered but returned no data for vector(1)"
    return True, "Prometheus reachable"


def check_targets_down():
    """Any Prometheus scrape target reporting up==0 (monitoring going blind)."""
    return targets_verdict(
        bridge_io.prom_vector("up%s" % bridge_io.origin_sel()), cfg.TARGETS_MIN
    )


def check_traefik_5xx():
    """Elevated 5xx ratio per Traefik service, naming each offender.

    Per-service (not aggregate) for two reasons: the alert points at *which* backend is
    erroring, and a broken low-traffic service can't hide diluted below the threshold by
    healthy high-traffic ones. The TRAEFIK_MIN_RPS floor is per-service too — same idea
    as before, a single error on a near-idle route is not a 100%-error-ratio alarm.
    """
    total_vec = bridge_io.prom_vector(
        "sum(rate(traefik_service_requests_total[5m])) by (service)"
    )
    err_rps = dict(
        (m.get("service", "?"), v)
        for m, v in bridge_io.prom_vector(
            'sum(rate(traefik_service_requests_total{code=~"5.."}[5m])) by (service)'
        )
    )
    offenders = []
    total_rps = 0.0
    eligible = 0
    for m, rps in total_vec:
        total_rps += rps
        if rps < cfg.TRAEFIK_MIN_RPS:
            continue
        eligible += 1
        svc = m.get("service", "?")
        pct = 100.0 * err_rps.get(svc, 0.0) / rps
        if pct > cfg.TRAEFIK_5XX_PCT:
            offenders.append((svc, pct, rps))
    offenders.sort(key=lambda spr: -spr[1])
    if offenders:
        desc = ", ".join("%s (%.0f%% of %.2f rps)" % o for o in offenders[:5])
        return False, "%d service(s) over %.0f%% 5xx: %s" % (
            len(offenders),
            cfg.TRAEFIK_5XX_PCT,
            desc,
        )
    return True, "5xx ok: %d service(s) above floor, %.2f rps total" % (
        eligible,
        total_rps,
    )


def check_traefik_latency():
    """Share of slow requests per Traefik service, naming each offender.

    The gap check_traefik_5xx cannot close: a slow route still answers 200, so an error-ratio
    check stays green while a service is unusable. Nothing here watched latency at all before
    this, so a backend could degrade indefinitely without any monitor noticing.

    Latency rather than the 499s that slow routes produce: a 499 only means the client
    disconnected first, which is also what happens every time someone closes a tab with
    in-flight requests. On a polling dashboard that is constant and entirely normal, so a
    499-based alert would page for ordinary browsing.

    Same shape as check_traefik_5xx deliberately: per-service so the alert names the offender,
    and behind the same TRAEFIK_MIN_RPS floor so one slow request on a near-idle route is not
    an alarm. It shares the 5xx check's ratio form too, for the reason in TRAEFIK_SLOW_BUCKET:
    a share of requests past a real bucket edge is exact where an interpolated quantile is not.

    Both rates come from the histogram's own series (_count, not traefik_service_requests_total)
    so numerator and denominator are always the same scrape of the same metric family — mixing
    the two counters lets a ratio drift outside 0-100% between scrapes.
    """
    total = dict(
        (m.get("service", "?"), v)
        for m, v in bridge_io.prom_vector(
            "sum(rate(traefik_service_request_duration_seconds_count[5m])) by (service)"
        )
    )
    under = dict(
        (m.get("service", "?"), v)
        for m, v in bridge_io.prom_vector(
            'sum(rate(traefik_service_request_duration_seconds_bucket{le="%s"}[5m])) '
            "by (service)" % cfg.TRAEFIK_SLOW_BUCKET
        )
    )
    offenders = []
    unmeasurable = []
    eligible = 0
    worst = 0.0
    for svc, rps in total.items():
        if rps < cfg.TRAEFIK_MIN_RPS:
            continue
        # A cumulative histogram emits every bucket for any service that served a request, so a
        # service missing from `under` means the le= selected nothing at all — Traefik's buckets
        # were reconfigured out from under this check. Report that rather than reading the
        # absent series as 0 requests under the boundary, which would page every service at once.
        if svc not in under:
            unmeasurable.append(svc)
            continue
        eligible += 1
        pct = 100.0 * (1.0 - under[svc] / rps)
        worst = max(worst, pct)
        if pct > cfg.TRAEFIK_SLOW_PCT:
            offenders.append((svc, pct, rps))
    if unmeasurable:
        return (
            False,
            "no %ss bucket for %d service(s) (%s) — check Traefik's histogram buckets"
            % (
                cfg.TRAEFIK_SLOW_BUCKET,
                len(unmeasurable),
                ", ".join(sorted(unmeasurable)[:5]),
            ),
        )
    offenders.sort(key=lambda spr: -spr[1])
    if offenders:
        desc = ", ".join("%s (%.0f%% of %.2f rps)" % o for o in offenders[:5])
        return (
            False,
            "%d service(s) with over %.0f%% of requests slower than %ss: %s"
            % (
                len(offenders),
                cfg.TRAEFIK_SLOW_PCT,
                cfg.TRAEFIK_SLOW_BUCKET,
                desc,
            ),
        )
    return True, "latency ok: %d service(s) above floor, worst %.1f%% over %ss" % (
        eligible,
        worst,
        cfg.TRAEFIK_SLOW_BUCKET,
    )


def check_k8s_workloads():
    """Deployment readiness for every workload in the k3s cluster.

    Gated by check_cluster_prometheus rather than the ordinary Prometheus gate: this is the one
    check reading the CLUSTER Prometheus, so the `prom_ok` gate is not watching its source. See
    CLUSTER_DEPENDENT.
    """
    if not cfg.CLUSTER_PROM_URL:
        return True, "k8s workload check disabled (no CLUSTER_PROMETHEUS_URL)"
    total = bridge_io.prom_scalar(
        "count(kube_deployment_status_replicas_unavailable)",
        base=cfg.CLUSTER_PROM_URL,
        source="cluster prometheus",
    )
    offenders = bridge_io.prom_vector(
        "kube_deployment_status_replicas_unavailable > 0",
        base=cfg.CLUSTER_PROM_URL,
        source="cluster prometheus",
    )
    # The second clause is the recency gate (K8S_RESTART_RECENT_WINDOW): it keeps a recovered
    # pod from holding the tile red for the rest of the 1h evidence window. `and` is a vector
    # match on the full label set, so it filters the first clause's series rather than
    # replacing them — the offender labels reaching the verdict are unchanged.
    restart_offenders = bridge_io.prom_vector(
        "increase(kube_pod_container_status_restarts_total[%s]) > %d"
        " and increase(kube_pod_container_status_restarts_total[%s]) > 0"
        % (cfg.K8S_RESTART_WINDOW, cfg.K8S_RESTART_MAX, cfg.K8S_RESTART_RECENT_WINDOW),
        base=cfg.CLUSTER_PROM_URL,
        source="cluster prometheus",
    )
    ds_total = bridge_io.prom_scalar(
        "count(kube_daemonset_status_number_unavailable)",
        base=cfg.CLUSTER_PROM_URL,
        source="cluster prometheus",
    )
    ds_offenders = bridge_io.prom_vector(
        "kube_daemonset_status_number_unavailable > 0",
        base=cfg.CLUSTER_PROM_URL,
        source="cluster prometheus",
    )
    ok, msg = k8s_workloads_verdict(
        total,
        offenders,
        cfg.K8S_MIN_WORKLOADS,
        restart_offenders,
        ds_total,
        ds_offenders,
        cfg.K8S_MIN_DAEMONSETS,
    )
    # Folded into this monitor rather than given its own: a new Kuma monitor needs a new push
    # token in SOPS, and this arm answers the same question the DaemonSet arm does — is the
    # cluster still able to run the workloads that depend on it.
    advertised = {}
    for resource in cfg.K8S_EXTENDED_RESOURCES:
        advertised[resource] = len(
            bridge_io.prom_vector(
                'kube_node_status_allocatable{resource="%s"} > 0'
                % ksm_resource_label(resource),
                base=cfg.CLUSTER_PROM_URL,
                source="cluster prometheus",
            )
        )
    res_ok, res_msg = extended_resource_verdict(
        cfg.K8S_EXTENDED_RESOURCES,
        advertised,
        bridge_io.prom_scalar(
            "count(kube_node_status_allocatable)",
            base=cfg.CLUSTER_PROM_URL,
            source="cluster prometheus",
        ),
    )
    if not res_ok:
        # The resource fault wins the message: an unschedulable-by-design cluster is more urgent
        # than whatever the workload arm has to say, and the workload arm's own text is preserved
        # after it rather than dropped.
        return checks_logs.with_log_errors(False, "%s | %s" % (res_msg, msg))
    return checks_logs.with_log_errors(ok, "%s, %s" % (msg, res_msg))


def check_cluster_targets():
    """Scrape targets of the CLUSTER's own Prometheus (the other half of Scrape Targets).

    B5 pinned check_targets_down to origin="daniel-server" so it kept meaning exactly what it
    always meant. The cost of that, unpaid until now, is that the cluster's own five targets were
    watched by nothing: cluster_prometheus probes only reachability, and k8s_workloads reads
    deployment replicas rather than scrape health. kube-state-metrics failing is covered by
    accident (its series vanish and the workload check fails closed on the floor), but
    otel-collector and otel-collector-internal going down was silent — and those two carry the
    only copy of Claude Code's session/token/cost telemetry.

    `origin!="daniel-server"` is everything the pinned sibling does NOT cover: cluster-native
    series, whose `origin` label is absent (PromQL treats an absent label as empty, so `!=` on a
    non-empty value matches them), plus daniel-box's own. The earlier `origin=""` caught only the
    first, which left `up{job="node",origin="daniel-box"}` matching NEITHER check — daniel-box's
    node-exporter could die watched by nothing. Complementary selectors, so every `up` series in
    this Prometheus belongs to exactly one of the two checks. The same floor logic as its sibling,
    so an emptied `up` reads as UNKNOWN rather than as nothing being wrong.
    """
    if not cfg.CLUSTER_PROM_URL:
        return True, "cluster target check disabled (no CLUSTER_PROMETHEUS_URL)"
    vec = bridge_io.prom_vector(
        'up{origin!="daniel-server"}',
        base=cfg.CLUSTER_PROM_URL,
        source="cluster prometheus",
    )
    return targets_verdict(vec, cfg.CLUSTER_TARGETS_MIN)


def check_cluster_prometheus():
    """Reachability gate for the cluster Prometheus — the peer of check_prometheus.

    Kept separate from the Docker Prometheus gate on purpose. They are different instances on
    different hosts reached by different paths, so one `prom_ok` cannot describe both: a gate
    that is not watching a check's actual source is worse than no gate, because it reports
    confidence it does not have.
    """
    if not cfg.CLUSTER_PROM_URL:
        return True, "cluster Prometheus check disabled (no CLUSTER_PROMETHEUS_URL)"
    value = bridge_io.prom_scalar(
        "vector(1)", base=cfg.CLUSTER_PROM_URL, source="cluster prometheus"
    )
    if value is None:
        return False, "cluster Prometheus returned no result for vector(1)"
    return True, "cluster Prometheus reachable"
