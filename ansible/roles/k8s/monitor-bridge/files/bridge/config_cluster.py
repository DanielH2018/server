"""The cluster-facing half of monitor-bridge's configuration.

The second Prometheus, the origin pin derived from whether the two Prometheus URLs name one
instance, the scrape-target and cAdvisor coverage floors, the kube-state-metrics workload
floors, Longhorn and the PVC fullness arm.

Most of these are FLOORS rather than limits, and the comments beside them say what an absent
series would otherwise read as. Field justifications sit beside the declarations, env var names
and defaults beside the reads. Composed into `Config` by `bridge/config.py`; imports nothing
from it.
"""

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class ClusterConfig:
    """The second Prometheus, the origin pin, the coverage floors, Longhorn and the PVC arm."""

    OOM_WINDOW: str
    CPU_WINDOW: str
    CPU_THROTTLE_PCT: float
    CPU_MIN_THROTTLED_CORES: float
    CPU_CONSECUTIVE: int
    RESTART_WINDOW: str
    RESTART_MAX: float
    CLUSTER_PROM_URL: str
    PROM_ORIGIN: str
    TARGETS_MIN: int
    CLUSTER_TARGETS_MIN: int
    CADVISOR_PODS_MIN: int
    CADVISOR_CONSECUTIVE: int
    K8S_MIN_WORKLOADS: int
    K8S_MIN_DAEMONSETS: int
    LONGHORN_CONSECUTIVE: int
    PVC_MAX_PCT: float
    PVC_MIN_CLAIMS: int
    PVC_CLAIMS_CONSECUTIVE: int
    K8S_RESTART_WINDOW: str
    K8S_RESTART_MAX: int
    K8S_RESTART_RECENT_WINDOW: str


def cluster_config(
    _env: Callable[..., str],
    _int: Callable[[str, str], int],
    _num: Callable[[str, str], float],
    prom_url: str,
) -> ClusterConfig:
    """The cluster fields, read through the parsers `load_config` built over its environment."""
    # PROM_ORIGIN is derived from whether the two Prometheus URLs name one
    # instance, so this is read ahead of the dict rather than inside it.
    CLUSTER_PROM_URL = _env("CLUSTER_PROMETHEUS_URL", "").rstrip("/")

    return ClusterConfig(
        CLUSTER_PROM_URL=CLUSTER_PROM_URL,
        OOM_WINDOW=_env("OOM_WINDOW", "1h"),
        CPU_WINDOW=_env("CPU_WINDOW", "15m"),
        CPU_THROTTLE_PCT=_num("CPU_THROTTLE_PCT", "25"),
        CPU_MIN_THROTTLED_CORES=_num("CPU_MIN_THROTTLED_CORES", "0.05"),
        CPU_CONSECUTIVE=_int("CPU_CONSECUTIVE", "3"),
        RESTART_WINDOW=_env("RESTART_WINDOW", "15m"),
        RESTART_MAX=_num("RESTART_MAX", "3"),
        # Which estate the host-health checks mean, when one Prometheus holds two (slice 3, B5).
        #
        # Since B3 the cluster Prometheus carries daniel-server's whole TSDB alongside its own,
        # tagged with `origin` (Prometheus external_labels). Three metric families genuinely
        # exist on BOTH sides — measured 2026-08-07: container_start_time_seconds (99
        # cluster-native / 53 here), container_memory_failcnt and the container_cpu_cfs_* pair,
        # plus `up` (5 / 11). So the moment PROMETHEUS_URL points at the cluster, restarts / oom
        # / cpu / janitorr / targets silently widen from "daniel-server's containers" to "every
        # container in the homelab", and would start naming k8s pods as offenders. The remaining
        # PROM_DEPENDENT checks that DON'T read traefik_* are pinned
        # (cert/restarts/oom/cpu/targets/ups/promtail_dropped). Disk and memory are the two
        # exceptions: they are HOST checks, and pinning them to one origin would leave the other
        # host's root disk and memory unwatched — so they group `by (origin)` and report the
        # worst, covering both. Since E2 the cluster edge also emits traefik_* (traefik-k8s
        # job), so the unpinned traefik/cert checks now deliberately read the CLUSTER edge's
        # metrics.
        #
        # `{name!=""}` does NOT already scope this, which is the obvious assumption and a wrong
        # one: the kubelet's cAdvisor emits `name` too, so 99 cluster-native series survive that
        # filter.
        #
        # DERIVED, not configured. The pin is required when reading the cluster copy and WRONG
        # when reading the Docker instance — whose own storage has no `origin` label at all,
        # because external_labels are applied on remote-write and never to local queries. A
        # compose variable that had to be flipped in lockstep with PROMETHEUS_URL is precisely
        # the drift this avoids: pointing one at the cluster and forgetting the other would
        # silently select nothing and read as healthy. The _env override stays so a third estate
        # is not blocked by the derivation.
        PROM_ORIGIN=_env(
            "PROM_ORIGIN",
            'origin="daniel-server"'
            if prom_url and prom_url == CLUSTER_PROM_URL
            else "",
        ),
        # Floor below which the `up` vector is treated as missing rather than clean — see
        # targets_verdict.
        # CORRECTED 2026-08-24: this said "exactly two origin="daniel-server" jobs: node,
        # cadvisor". It is ONE — `node`. Only the node job is relabelled with `origin`
        # (claude-otel/templates/prometheus.yaml.j2:202, the `node` job); the cadvisor job never
        # was, which is the whole mechanism behind the blind restarts/oom/cpu checks fixed the
        # same day. A reviewer checking that finding against this comment would have cleared it,
        # so the stale half is corrected here rather than left to be re-derived.
        # The deployed TARGETS_MIN is 1 (env-secret.yaml.j2), which matches that single job and
        # still fails closed: targets_verdict tests `len(vec) < min_targets`, so an empty vector
        # is 0 < 1 and reports UNKNOWN. A floor of 1 cannot detect a PARTIAL shortfall, but with
        # one expected series there is no partial case to detect. The code default of 2 is kept
        # only as the fail-safe for a host whose env omits the key entirely.
        TARGETS_MIN=_int("TARGETS_MIN", "2"),
        # Same floor idea for the cluster's own scrape targets (see check_cluster_targets).
        # Since the otel-collector became a DaemonSet (Phase F drain, 2026-08-13) its two jobs
        # are per-POD — one target per node each — so the set is seven: prometheus, 2x
        # otel-collector, 2x otel-collector-internal, kube-state-metrics, kubernetes-cadvisor.
        # 3 still tolerates a deliberate removal without ever mistaking an empty vector for a
        # clean one.
        CLUSTER_TARGETS_MIN=_int("CLUSTER_TARGETS_MIN", "3"),
        # Coverage floor for the three cAdvisor checks (restarts/oom/cpu), which filter a
        # per-pod vector down to offenders and so cannot tell "quiet" from "gone". Reasoning and
        # the measurements behind the value: cadvisor_coverage_shortfall in verdicts/cluster.py.
        CADVISOR_PODS_MIN=_int("CADVISOR_PODS_MIN", "20"),
        # Hysteresis for the same reason HOST_ORIGINS_CONSECUTIVE exists: a kubelet restart
        # takes a node's cAdvisor away briefly, and three monitors going down together on one
        # transient is the alert storm the gates elsewhere in this file exist to prevent.
        CADVISOR_CONSECUTIVE=_int("CADVISOR_CONSECUTIVE", "2"),
        # The floor below which the deployment series is treated as missing rather than healthy.
        # THE FAILURE THIS EXISTS TO PREVENT: an absent series makes `unavailable > 0` return an
        # empty vector, which reads exactly like "nothing is unavailable" — green, silent, and
        # wrong, the same shape as the B2 transaction cap (2026-08-02) and the gitops-behind
        # defer (2026-08-07). So the check COUNTS the series first and fails closed when the
        # count is short, instead of inferring health from an empty result. The floor also
        # covers a partially-loaded kube-state-metrics: its ClusterRole is deliberately scoped,
        # so dropping `apps` from it would take every deployment series away while the pod stays
        # up and Ready.
        K8S_MIN_WORKLOADS=_int("K8S_MIN_WORKLOADS", "5"),
        # Same fail-closed reasoning as K8S_MIN_WORKLOADS, for the DaemonSet series
        # (kube_daemonset_status_number_unavailable) instead of the Deployment one — a
        # DaemonSet's absent/unschedulable pod has no Deployment-arm equivalent, so it was
        # invisible until this arm existed. The nine DaemonSets running as of 2026-08-13:
        # otel-collector, promtail, scrutiny-collector, crowdsec-node-agent, dri-device-plugin,
        # engine-image-*, longhorn-csi-plugin, longhorn-manager, speaker. Bump this floor (and
        # the comment) when a DaemonSet is added or retired — same discipline as
        # K8S_MIN_WORKLOADS.
        K8S_MIN_DAEMONSETS=_int("K8S_MIN_DAEMONSETS", "9"),
        # Hysteresis for check_longhorn_volumes. A node drain and the Sunday 07:30 reboot both
        # degrade every volume on the departing node BY DESIGN, so a single breaching cycle must
        # not page — 3 cycles at the bridge cadence is longer than either takes to settle. Same
        # shape as CPU_CONSECUTIVE / UPS_CONSECUTIVE.
        LONGHORN_CONSECUTIVE=_int("LONGHORN_CONSECUTIVE", "3"),
        # Filesystem fullness of the cluster's PersistentVolumeClaims (check_pvc_fullness). A
        # separate arm from check_disk rather than another DISK_MOUNTPOINTS entry: a Longhorn
        # PVC is its own filesystem at a FIXED capacity, so it cannot borrow the host's free
        # space and a full one is invisible to every mountpoint query. 85 rather than
        # DISK_MAX_PCT's 90 because a PVC cannot be grown by deleting something elsewhere — the
        # operator has to expand the volume, and the alert has to arrive while that is still
        # unhurried work. Measured 2026-09-01: the fullest claim was uptime-kuma-data at 38.6%,
        # and the smallest genuine claim is 973 MiB, where 85% leaves 146 MiB of headroom
        # against 97 MiB at 90%.
        PVC_MAX_PCT=_num("PVC_MAX_PCT", "85"),
        # Coverage floor, in CLAIMS not series — see check_pvc_fullness for why the two differ.
        # NOT a conservative under-count like CADVISOR_PODS_MIN, because the degraded state here
        # is a specific number rather than an empty vector. The two scrape jobs cover unequally
        # (measured 2026-09-01, groups not series): kubelet alone reports all 43 claims,
        # apiserver alone reports 27. So the apiserver job dying costs no coverage at all, and
        # the ONLY hazard is the kubelet job dying, which leaves 27 claims answering while
        # daniel-server's go dark. A floor at or under 27 reads that as healthy — the same
        # partial blindness HOST_ORIGINS_MIN exists for. 32 is strictly above the 27-claim
        # survivor and 11 below the live 43, so it fires on that outage and still tolerates a
        # dozen services being retired.
        PVC_MIN_CLAIMS=_int("PVC_MIN_CLAIMS", "32"),
        # Hysteresis on the coverage floor only. A kubelet restart or a node drain drops a
        # node's volume stats for a cycle or two, and that must not page; a fullness breach gets
        # no grace because it is monotonic rather than flappy.
        PVC_CLAIMS_CONSECUTIVE=_int("PVC_CLAIMS_CONSECUTIVE", "3"),
        # Crash-loop arm of the workload check: pods whose restart counter climbed more than
        # K8S_RESTART_MAX inside K8S_RESTART_WINDOW page even while readiness flaps green
        # (CrashLoopBackOff passes probes briefly each backoff cycle — the 2026-08-13 homepage
        # incident: 31 restarts overnight, tile and replica check mostly green throughout).
        # 3-in-1h ≈ steady-state backoff cadence; a legitimate deploy rollout restarts once.
        K8S_RESTART_WINDOW=_env("K8S_RESTART_WINDOW", "1h"),
        K8S_RESTART_MAX=_int("K8S_RESTART_MAX", "3"),
        # Recency gate on the same arm: `increase(...[1h])` is a pure lookback, so a pod that
        # crash-looped and then RECOVERED keeps the monitor DOWN until the restarts age out of
        # the 1h window — up to an hour of red on a healthy pod (2026-08-23 zigbee2mqtt:
        # recovered 09:47, arm still firing on `restarts in window: 9`). Requiring a restart
        # inside the last K8S_RESTART_RECENT_WINDOW as well clears the tile ~30m after the pod
        # steadies while leaving the 1h evidence base untouched — an ongoing loop always has a
        # recent restart.
        #
        # DECIDED: 30m, and the floor is the worst inter-restart SPACING, not the
        # CrashLoopBackOff 5-min backoff cap. The 2026-08-13 homepage incident above spread 31
        # restarts over a night, ~15-19 min apart; a window inside that spacing goes UP in the
        # gaps and flaps — and `k3s Workload Health` is `max_retries: 0`
        # (uptime-kuma static-monitors.yaml.j2:293), so every flap is an immediate DOWN plus a
        # notification. That is the crowdsec-appsec failure recorded at
        # static-monitors.yaml.j2:283-289 (24 transitions in 3h). 30m clears two spacings and
        # six bridge cycles. The spacing could not be re-measured — cluster Prometheus retains
        # 7d and the incident is older — so this is the conservative floor.
        K8S_RESTART_RECENT_WINDOW=_env("K8S_RESTART_RECENT_WINDOW", "30m"),
    )
