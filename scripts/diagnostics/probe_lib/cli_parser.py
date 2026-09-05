"""probe.py's argparse surface: every subparser, plus the `cert` openssl stage builder.

Split out of probe.py, which had grown to 697 lines. `_build_parser` is the whole of that
file's argparse; `cert_stages` sits beside it because the `cert` subparser is the only caller
that shapes its arguments.

`curl_pipeline.py` imports this module to parse and route; nothing here imports `curl_pipeline.py` or
probe.py, so the dependency runs one way.
"""

import argparse

# `probe_lib` is a namespace package under `scripts/`, so reaching a sibling by package name
# needs `scripts/` on sys.path — a module gets only its importer's path otherwise, and
# pyproject's `pythonpath` is a pytest setting. This has to sit ABOVE the imports below.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from diagnostics.probe_lib.arr import ARR_PORTS
from diagnostics.probe_lib.longhorn import LONGHORN_PREFIX


def cert_stages(host, port, sni):
    """Open a TLS session with SNI and decode the served leaf cert.

    Two stages: the session, then the cert's subject, issuer and validity. Read-only — no data
    is sent.

    NB: connects to whatever DNS resolves `host` to. For a Cloudflare-proxied public host that's the
    CF edge (→ the Cloudflare edge cert), NOT Traefik's origin cert — pass the origin IP as the
    target with `--sni <host>` to inspect the origin Let's Encrypt cert.
    """
    s_client = [
        "openssl",
        "s_client",
        "-connect",
        f"{host}:{port}",
        "-servername",
        sni,
        "-verify_hostname",
        sni,
    ]
    x509 = [
        "openssl",
        "x509",
        "-noout",
        "-subject",
        "-issuer",
        "-dates",
        "-fingerprint",
        "-sha256",
    ]
    return [s_client, x509]


# routing (pure given resolve_ip)


def _build_parser():
    p = argparse.ArgumentParser(
        prog="probe.py", description="read-only homelab diagnostics"
    )
    p.add_argument(
        "--dry-run", action="store_true", help="print the command(s) instead of running"
    )
    p.add_argument(
        "--list",
        action="store_true",
        help="list every subcommand with its description and exit "
        "(handled before subcommand parsing, so it needs no subcommand)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("metric", help="Prometheus instant query")
    m.add_argument("promql")
    m.add_argument(
        "--json",
        action="store_true",
        help="print raw JSON instead of the formatted view",
    )
    tg = sub.add_parser("targets", help="Prometheus scrape-target health")
    tg.add_argument(
        "--pi",
        action="store_true",
        help="scope to daniel-pi's own scrape jobs (derived from the k8s_pi_client_ip static "
        "targets in claude-otel's prometheus.yaml.j2), formatted rather than raw JSON, and "
        "exits 1 if a declared job has gone missing, not just down",
    )
    sub.add_parser("monitors", help="Kuma down-monitors rollup (exit 0 = all up)")
    kd = sub.add_parser(
        "kuma-drift",
        help="declared monitors vs live ones — catches a tile that is gone rather "
        "than down, which `monitors` counts as green (exit 0 = no drift)",
    )
    kd.add_argument(
        "--no-secrets",
        action="store_true",
        help="skip the SOPS reads that resolve why a gated monitor is absent; those gates "
        "report as unverified instead of gated/ungated",
    )
    kd.add_argument(
        "--pi",
        action="store_true",
        help="scope to daniel-pi's declared monitors (by their static-monitors.yaml.j2 key) "
        "instead of the whole cluster's",
    )
    sub.add_parser("loki-labels", help="Loki label names")
    lq = sub.add_parser("loki-query", help="Loki range query")
    lq.add_argument("logql")
    lq.add_argument("--limit", type=int, default=100)
    lq.add_argument(
        "--since",
        help="how far back to look, e.g. 30m/6h/2d/1w. Loki defaults to ONE HOUR, and "
        "anything older simply returns nothing — an empty result reads as 'no logs' rather "
        "than 'you did not ask far enough back'.",
    )
    lq.add_argument(
        "--json", action="store_true", help="print raw JSON instead of the log lines"
    )
    al = sub.add_parser(
        "alerts", help="monitor-bridge DOWN alert history, collapsed to episodes (Loki)"
    )
    al.add_argument(
        "--days", type=float, default=7, help="lookback window in days (default 7)"
    )
    al.add_argument("--check", help="filter to check names containing this substring")
    al.add_argument(
        "--pi",
        action="store_true",
        help="scope to alerts attributable to daniel-pi: the syslog stream's own host token, "
        "or monitor-bridge's pi_pressure check (the only one that watches the Pi remotely)",
    )
    al.add_argument(
        "--gap-min",
        type=float,
        help="minutes of silence that splits one episode from the next. Left unset, each "
        "check gets its own threshold from its own sample cadence — a fixed 30 matched the "
        "*/30 crons exactly and split every continuous outage into one episode per tick",
    )
    al.add_argument("--limit", type=int, default=5000)
    al.add_argument(
        "--raw", action="store_true", help="print the matching log lines, not episodes"
    )
    al.add_argument(
        "--json", action="store_true", help="print episodes as JSON (epoch-ns times)"
    )
    sub.add_parser("scrutiny", help="disk SMART summary")
    b2l = sub.add_parser(
        "b2-longhorn",
        help="Longhorn backup objects in B2, per volume — proves DATA blocks landed, "
        "not just metadata (exit 1 if any volume has none)",
    )
    b2l.add_argument(
        "--bucket", help="override the bucket read from the kopia_b2_bucket secret"
    )
    b2l.add_argument(
        "--prefix", default=LONGHORN_PREFIX, help="B2 prefix Longhorn writes under"
    )
    b2b = sub.add_parser(
        "b2-budget",
        help="per-shard Class C projection against B2's free-tier daily cap "
        "(exit 1 if a weekly shard is over budget)",
    )
    b2b.add_argument(
        "--bucket", help="override the bucket read from the kopia_b2_bucket secret"
    )
    b2b.add_argument(
        "--prefix", default=LONGHORN_PREFIX, help="B2 prefix Longhorn writes under"
    )
    b2b.add_argument(
        "--retain",
        type=int,
        default=2,
        help="k3s_longhorn_weekly_backup_retain, to spot backups no job will prune",
    )
    b2s = sub.add_parser(
        "b2-spend",
        help="MEASURED Class B spend from Longhorn's own logs (b2-budget projects Class C; "
        "this counts what backups actually cost). Reads Loki, spends nothing on B2.",
    )
    b2s.add_argument("--since", default="24h", help="window, e.g. 30m/6h/2d/1w")
    b2s.add_argument("--limit", type=int, default=1000)
    b2r = sub.add_parser(
        "b2-record",
        help="record a tool's B2 transaction spend in today's ledger, so maintenance "
        "spend stops being reconstructed from memory",
    )
    b2r.add_argument(
        "--tool", required=True, help="what spent them, e.g. drain, inventory"
    )
    b2r.add_argument("--class-a", type=int, default=0, dest="class_a")
    b2r.add_argument("--class-b", type=int, default=0, dest="class_b")
    b2r.add_argument("--class-c", type=int, default=0, dest="class_c")
    b2r.add_argument("--note", default="")
    b2d = sub.add_parser(
        "b2-deletions",
        help="charge completed Longhorn backup deletions to today's ledger, priced from the "
        "block-tree counts the last b2-budget listing wrote. Reads Loki and the Kubernetes "
        "API; spends nothing on B2.",
    )
    b2d.add_argument("--since", default="26h", help="window, e.g. 30m/6h/2d/1w")
    b2d.add_argument("--limit", type=int, default=1000)
    b2d.add_argument(
        "--target-url",
        dest="target_url",
        default="",
        help="B2 backupTargetURL to charge against, when the BackupTarget CR cannot be read "
        "(it is disarmed, or kubectl is unavailable)",
    )
    b2d.add_argument(
        "--no-record",
        action="store_true",
        dest="no_record",
        help="report what would be charged without writing the ledger",
    )
    lb = sub.add_parser(
        "longhorn-blocks",
        help="census live Longhorn volumes by tier and backup block size; exit 1 when a "
        "weekly-shard volume is not on 16 MiB blocks. Reads the cluster, spends no B2.",
    )
    lb.add_argument(
        "--dry-run",
        action="store_true",
        help="print the kubectl call without making it",
    )
    rb = sub.add_parser(
        "readonly-rbac",
        help="assert plain kubectl is still read-only — exit 1 on privilege creep, 2 when the "
        "control verbs are refused and nothing can be concluded. Uses `auth can-i`, writes "
        "nothing.",
    )
    rb.add_argument(
        "--namespace", help="namespace to ask about (default: the workload one)"
    )
    rb.add_argument(
        "--dry-run",
        action="store_true",
        help="print the can-i calls without making them",
    )
    vp = sub.add_parser(
        "vip-placement",
        help="assert every ETP=Local MetalLB VIP has a Ready endpoint on the node that "
        "announces it — exit 1 when one is stranded (the announcer DROPs its traffic while "
        "the Service reads healthy), 2 when the read came back empty. A VIP whose workloads "
        "all declare zero replicas is reported scaled-to-zero, not stranded.",
    )
    vp.add_argument(
        "--dry-run",
        action="store_true",
        help="print the kubectl calls without making them",
    )
    pi = sub.add_parser("pi", help="Pi glances API")
    pi.add_argument(
        "subpath",
        help="glances API path (e.g. fs, quicklook, mem, cpu), or `containers` for a "
        "one-ssh docker inspect view of every container on the host",
    )
    ct = sub.add_parser(
        "cert",
        help="served TLS cert details (public hosts show the CF edge cert — "
        "pass the origin IP + --sni for the origin cert)",
    )
    ct.add_argument("target", help="host or host:port")
    ct.add_argument("--sni", help="SNI servername (defaults to host)")
    hl = sub.add_parser(
        "health", help="k8s rollout + restart rollup (exit 0 = healthy)"
    )
    hl.add_argument("container", help="service name, e.g. jellyfin")
    hl.add_argument(
        "--docker",
        action="store_true",
        help="inspect the Pi's Docker container instead of a k8s Deployment",
    )
    ar = sub.add_parser(
        "arr", help="read-only *arr API GET (key from SOPS, fed via stdin)"
    )
    ar.add_argument("app", choices=sorted(ARR_PORTS))
    ar.add_argument("path", help="api path, e.g. health, indexerstatus, notification")
    ar.add_argument(
        "--json", action="store_true", help="print raw JSON instead of pretty-printed"
    )
    ha = sub.add_parser("ha", help="Home Assistant live state (read-only, GET)")
    hasub = ha.add_subparsers(dest="ha_cmd", required=True)
    hs = hasub.add_parser("state", help="GET /api/states/<entity_id>")
    hs.add_argument("entity_id", help="e.g. fan.tower_fan")
    hs.add_argument("--json", action="store_true", help="print raw JSON")
    hauto = hasub.add_parser(
        "automation", help="one automation by id, alias-slug, or entity_id"
    )
    hauto.add_argument(
        "query", help="automation id, alias-slug, or full automation.<slug>"
    )
    hauto.add_argument("--json", action="store_true", help="print raw JSON")
    hg = hasub.add_parser("get", help="raw GET /api/<path>, e.g. error_log")
    hg.add_argument("path")
    htr = hasub.add_parser(
        "trace",
        aliases=["why"],
        help="why an automation last ran/no-op'd (per-condition WS trace)",
    )
    htr.add_argument(
        "query", help="automation id, alias-slug, or full automation.<slug>"
    )
    hasub.add_parser(
        "verify-automations",
        help="assert every automation in files/automations/ loaded (exit 0 = all loaded)",
    )
    hasub.add_parser(
        "verify-entities",
        help="assert every entity in state/external_entities.yml still exists live",
    )
    hst = sub.add_parser("ha-state", help="live view of the derived state model")
    hst.add_argument(
        "--inventory",
        action="store_true",
        help="also dump every live entity grouped by domain",
    )
    rel = sub.add_parser(
        "releases",
        help="which commit produced each k8s service's applied manifests",
    )
    rel.add_argument(
        "service",
        nargs="?",
        help="show the full record for one service instead of the table",
    )
    rel.add_argument(
        "--previous",
        action="store_true",
        help="read the record kept from before the last deploy",
    )
    rel.add_argument("--json", action="store_true", help="raw records, unformatted")
    rel.add_argument(
        "--stale-only",
        action="store_true",
        help=(
            "one line per service whose role paths moved past its applied commit, or with no "
            "record at all; exit 1 if any (for a cron -- see issue #947)"
        ),
    )
    return p
