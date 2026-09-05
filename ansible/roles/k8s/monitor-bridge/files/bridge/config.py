"""Env-derived configuration for monitor-bridge — every threshold, URL, credential and window.

One frozen `Config`, built once by `load_config(os.environ)` in check.py's `main()` and passed
down as the first parameter of every check body, gate and net helper. Nothing here is read from
a module global, so nothing here can be patched: a test builds the config it wants with
`load_config({...})` or narrows a fixture with `dataclasses.replace(cfg, X=...)`, and what the
code under test reads is the object it was handed.

That replaces the module-constant-plus-`monkeypatch` arrangement this file carried until
2026-09-04, where 118 test sites patched attributes on this module and two more `importlib
.reload()`d it to re-derive `PROM_ORIGIN` from a different environment. A patched module
constant is a process-wide global with a test-shaped hole in it; a parameter is not.

THE FIELDS LIVE IN FOUR DOMAIN MODULES, composed here by inheritance so that flat `cfg.X`
access survives: `config_host` (disks, SMART, temperatures, the UPS, the Pi), `config_service`
(Traefik, n8n, the *arr stack, the deployer, Home Assistant), `config_cluster` (the second
Prometheus, the coverage floors, Longhorn, PVCs) and `config_io` (B2, R2, Loki, the webhooks).
Each declares a frozen dataclass and a builder taking `load_config`'s parsers as arguments, so
a leaf never imports this facade. What stays HERE is the loop and endpoint fields every domain
needs, the check filter, and the two reads a repo test greps for by text:
`ansible/tests/services/test_dri_resource_name_agrees.py` matches
`_env("K8S_EXTENDED_RESOURCES", ...)` in THIS file, and `PVC_EXCLUDE` is rendered in
`templates/env-secret.yaml.j2` beside it.

BUILDING THIS MUST NOT RAISE. A ValueError on one malformed number used to kill the pod during
import, before the heartbeat file existed and before any monitor could be told. `_int`/`_num`
below record the problem, fall back to the documented default, and hand the whole list to
`main()` as `Config.CONFIG_PROBLEMS`, which prints one operator-readable line per problem and
exits 2.

Constants only. The mutable per-check state (`_n8n_streaks`, `_cadvisor_streaks`,
`_host_origin_streaks`, `_down_streaks`) stays with the code that mutates it.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from bridge.config_cluster import ClusterConfig, cluster_config
from bridge.config_host import HostConfig, host_config
from bridge.config_io import IoConfig, io_config
from bridge.config_service import ServiceConfig, service_config


@dataclass(frozen=True)
class Config(HostConfig, ServiceConfig, ClusterConfig, IoConfig):
    """One process's configuration, as read from the environment by `load_config`.

    Frozen, and built once in `main()`. The four bases contribute the domain fields; what is
    declared here is what every domain needs or what has to stay in this file by name. Each
    field is documented at its read — that is where the env var name and the default sit, and
    most of the reasoning is about the default rather than the type.

    EVERY CREDENTIAL-BEARING FIELD IS `field(repr=False)` in its domain module. A dataclass
    generates a `__repr__` over all of its fields, so without that a `print(cfg)`, an f-string
    in a log line, or a traceback rendering locals would put the HA token, both B2 keys, the
    Cloudflare token, the SMTP password, five Discord webhook URLs and five *arr API keys into
    the pod log — which promtail ships to Loki. `test_repr_hides_every_credential_but_not_
    ordinary_config` pins both halves: the secrets absent, and ordinary config still present.

    Attributes:
      CONFIG_PROBLEMS: one operator-readable line per env value that could not be parsed.
        Empty on a good config. Collecting them instead of raising is what keeps building this
        object safe at any point in startup; `main()` is where the list becomes an exit-2
        report.
    """

    INTERVAL: int
    GRACE_CYCLES: int
    HEARTBEAT_FILE: str
    PROM_URL: str
    KUMA_URL: str
    LOKI_URL: str
    K8S_EXTENDED_RESOURCES: tuple[str, ...]
    PVC_EXCLUDE: tuple[str, ...]
    CHECKS_ONLY: frozenset[str]
    CHECKS_SKIP: frozenset[str]
    CONFIG_PROBLEMS: tuple[str, ...] = ()


def load_config(env: Mapping[str, str], problems: list[str] | None = None) -> Config:
    """Read `env` into a frozen `Config`. Never raises.

    Args:
      env: The environment to read. `os.environ` in the pod; a plain dict in a test, which is
        how a test states the whole configuration a check runs under instead of patching one
        constant at a time.
      problems: Malformed-value lines already collected elsewhere, carried into
        `Config.CONFIG_PROBLEMS` so `main()` reports them together. `bridge.common
        .CONFIG_PROBLEMS` is what check.py passes: `HTTP_TIMEOUT` is parsed there (it is shared
        with autofix-bridge, which does not ship this module), and without this its malformed
        value would be recorded into a list nothing reads.

    Returns:
      The `Config` every check, gate and net helper is then handed.
    """
    problems = [] if problems is None else list(problems)

    def _env(name: str, default: str = "") -> str:
        return env.get(name, default)

    def _int(name: str, default: str) -> int:
        """The `name` env var as an int, recording a malformed value instead of raising."""
        raw = _env(name, default)
        try:
            return int(raw)
        except ValueError:
            problems.append(
                "%s=%r is not an integer; falling back to %s" % (name, raw, default)
            )
            return int(default)

    def _num(name: str, default: str) -> float:
        """The `name` env var as a float, recording a malformed value instead of raising."""
        raw = _env(name, default)
        try:
            return float(raw)
        except ValueError:
            problems.append(
                "%s=%r is not a number; falling back to %s" % (name, raw, default)
            )
            return float(default)

    def _env_file(name: str, default: str = "") -> str:
        """A secret from the file named by <name>_FILE if set, else the plain <name> env var.

        Inlined in the container environment, a secret lands in the pod's env, which envFrom
        cannot filter per key. Pointing <name>_FILE at a 0600 mounted file keeps it out of
        every process's environment (2026-07-15 review H2). Trailing whitespace is stripped so
        a rendered newline can't corrupt the value.

        A read error (the file went missing, or the mount source was auto-created as a
        directory because the host file was absent) falls back to the plain env var rather
        than raising: an unguarded open() would fail the whole config build over one missing
        file, disabling every check instead of just the one whose credential is empty
        (2026-07-15 review L1).
        """
        path = env.get(name + "_FILE", "")
        if path:
            try:
                with open(path, encoding="utf-8") as fh:
                    return fh.read().strip()
            except OSError:
                pass
        return _env(name, default)

    def _name_set(value: str) -> frozenset[str]:
        """A comma-separated check-name list as a set, tolerating spaces and empty entries."""
        return frozenset(n for n in value.replace(" ", "").split(",") if n)

    # Read ahead of the Config() call because another field derives from them:
    # B2_TRANSPORT_RETRY_S defaults to INTERVAL, and PROM_ORIGIN is derived from whether the
    # two Prometheus URLs name the same instance.
    INTERVAL = _int("INTERVAL", "300")
    PROM_URL = _env("PROMETHEUS_URL", "http://prometheus:9090").rstrip("/")

    return Config(
        **vars(host_config(_env, _int, _num, _env_file, problems)),
        **vars(service_config(_env, _int, _num, _env_file)),
        **vars(cluster_config(_env, _int, _num, PROM_URL)),
        **vars(io_config(_env, _int, _num, _env_file, INTERVAL)),
        INTERVAL=INTERVAL,
        # Startup/redeploy grace for the reach-out checks (STARTUP_GRACE, applied in run_once).
        # The bridge's first cycle after a host reboot runs before the heavy apps it polls (n8n,
        # sonarr/radarr, prowlarr, scrutiny, the Pi glances) finish starting, so an un-graced
        # reach-out check flips its max_retries=0 push monitor DOWN on that one transient cycle
        # and pages, then recovers next cycle — the weekly-reboot noise. Like HA_CONSECUTIVE,
        # only the GRACE_CYCLES'th consecutive down pages; a genuinely-down dependency still
        # alerts after ~one extra INTERVAL, and one ok resets the streak.
        GRACE_CYCLES=_int("GRACE_CYCLES", "2"),
        # Touched after every completed cycle; the container healthcheck compares its mtime
        # against ~3×INTERVAL. PID death already restarts the container, but a HANG only shows
        # up as push silence in Kuma — the healthcheck lets autoheal restart on that too.
        HEARTBEAT_FILE=_env("HEARTBEAT_FILE", "/tmp/heartbeat"),
        PROM_URL=PROM_URL,
        KUMA_URL=_env("KUMA_URL", "http://uptime-kuma:3001").rstrip("/"),
        LOKI_URL=_env("LOKI_URL", "http://loki:3100").rstrip("/"),
        # Extended resources that must stay ADVERTISED by at least one node. The DaemonSet arm
        # above watches whether the plugin's POD is running; this watches whether the thing the
        # pod exists to provide is still there. dri-device-plugin has no probe — and a container
        # with no readinessProbe is Ready the instant it starts — so a plugin that wedges
        # internally (gRPC registration hangs, a stuck goroutine) keeps a Running, Ready, fully-
        # available DaemonSet while kubelet quietly deregisters the resource. Nothing restarts
        # it, and the only other evidence is jellyfin and tdarr turning unschedulable, which
        # does not surface until they next reschedule.
        #
        # Comma-separated, so a second device plugin needs no code change. The literal `_env`
        # read is what ansible/tests/services/test_dri_resource_name_agrees.py greps for; keep
        # its text if this line moves.
        K8S_EXTENDED_RESOURCES=tuple(
            r.strip()
            for r in _env("K8S_EXTENDED_RESOURCES", "devic.es/dri").split(",")
            if r.strip()
        ),
        # Claims this arm must NOT scan, comma-separated bare claim names. media-data is a
        # `local` PV at /srv/media on daniel-box (k8s/media-volume/templates/pv.yaml.j2), i.e.
        # the `/` filesystem check_disk already watches — including it would page twice for one
        # full disk. Every other claim is Longhorn with a filesystem of its own. Keep this short
        # and say WHY in the inventory, like LOG_ERROR_IGNORE: a growing exclusion list means
        # the arm is decaying.
        PVC_EXCLUDE=tuple(
            c.strip() for c in _env("PVC_EXCLUDE", "media-data").split(",") if c.strip()
        ),
        # Which checks THIS instance runs. The Phase F twin/remnant split ended with the Docker
        # uninstall (2026-08-14): the cluster deployment is now the ONLY bridge and runs every
        # check (the gitops checks re-pointed at daniel-box's deployer via a hostPath — the pod
        # is pinned there; disk_prune retired with the Docker daemon; pi_peers/renovate_alive
        # became direct pushers at the host flips). The CHECKS_ONLY/CHECKS_SKIP mechanism stays —
        # it is how any future split would be expressed, and check.py's guards keep it honest.
        # CHECKS_ONLY (comma-separated names) enables exactly that set; CHECKS_SKIP drops names
        # from whatever is otherwise enabled. The four reachability gates participate under the
        # names their monitors push as (prometheus, loki_reachable, b2_reachable,
        # cluster_prometheus). A filter that enables a gated check while disabling its gate would
        # reintroduce the alert storm the gate exists to prevent, so check.py's main() refuses to
        # start on one (validate_check_filter) — a crash-looping bridge is loud, a mis-gated one
        # lies quietly.
        #
        # They live here rather than in check.py because this module is monitor-bridge's one
        # env-reading surface, and these were two of the three that had escaped it.
        # `check.py --check <name>` overrides CHECKS_ONLY for a hand-run cycle.
        CHECKS_ONLY=_name_set(_env("CHECKS_ONLY", "")),
        CHECKS_SKIP=_name_set(_env("CHECKS_SKIP", "")),
        CONFIG_PROBLEMS=tuple(problems),
    )
