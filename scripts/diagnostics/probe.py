#!/usr/bin/env python3
"""Read-only homelab diagnostics — one allow-listed surface for the queries that
used to be hand-written `curl`/`openssl` one-offs.

The monitoring stack (Prometheus, Loki, Scrutiny, uptime-kuma) does NOT publish
host ports; it's internal to the Docker network. The old approach was to
`curl http://<bridge-ip>:<port>/...` against a hand-copied container IP — but
Docker reassigns those IPs on recreate, so every such allow-list entry was dead
on the next deploy. This wrapper resolves the container's *current* IP via
`docker inspect` at run time, so it keeps working, and a single allow-list entry
covers every subcommand:

    Bash(uv run python scripts/diagnostics/probe.py:*)

Everything it runs is read-only (HTTP GET / TLS handshake / docker inspect).

Subcommands:
    metric '<promql>'        Prometheus instant query [--json] (prometheus :9090)
    targets                  Prometheus scrape-target health (prometheus :9090)
    monitors                 Kuma down-monitors rollup (exit 0 = all up), read from the
                             monitor_status metric Prometheus already scrapes — no Kuma
                             API credential needed
    loki-labels              Loki label names                (cluster loki-homelab)
    loki-query '<logql>'     Loki range query [--limit N] [--json] (cluster loki-homelab)
    alerts                   monitor-bridge DOWN history as episodes [--days N --check X --raw --json]
    scrutiny                 Disk SMART summary              (cluster scrutiny, both nodes)
    pi <subpath>             Pi glances API, e.g. `pi fs`    (daniel-pi.lan:61208)
    cert <host[:port]>       Served TLS cert subj/dates [--sni NAME]
    health <service>         k8s rollout + recent-restart rollup (exit 0 = healthy)
                             [--docker inspects the Pi's container instead]
    arr <app> <api-path>     Read-only *arr API GET [--json] (sonarr/radarr/prowlarr)
    ha state <entity_id>     Live HA entity state + attrs    (home-assistant :8123)
    ha automation <id|alias> One automation's on/off + last_triggered (resolves alias!=id)
    ha get <api-path>        Raw GET /api/<path>, e.g. `ha get error_log`
    ha trace <id|alias>      Why an automation last ran/no-op'd (per-condition WS trace; alias: why)
    ha verify-automations    Assert every automation in automations.yaml loaded (exit 0 = all loaded)
    ha verify-entities       Assert every entity in external_entities.yml still exists live
    ha-state [--inventory]   Live view of the derived HA state model

`metric` and `loki-query` print a formatted view by default (one `<labels> = <value>`
line per series; log lines oldest→newest) so you don't need to pipe into `python3 -c`
to reshape the JSON — pass `--json` for the raw response.

`ha` is read-only (GET) and authenticates with the SOPS-encrypted claude_ha_token
(server-only — needs the host age key). The token is fed to curl via stdin, never argv.
`arr` works the same way — it pulls `<app>_api_key` from SOPS and passes it via stdin,
so the *arr key never lands in argv / `ps` / shell history.
Add `--dry-run` to print the command(s) instead of running them.

NB: `cert <public-host>` shows the CLOUDFLARE EDGE cert, NOT Traefik's origin cert — public DNS
resolves the host to Cloudflare, so the TLS handshake terminates there. To inspect the origin
Let's Encrypt cert, point at the origin IP with the host as SNI:
`cert <server-ip>:443 --sni <host>` (origin expiry is also independently watched by
monitor-bridge's TLS Cert Expiry monitor).
"""

import argparse
import subprocess
import sys

# `core.<name>` for anything the tests monkeypatch — fetch, k8s_namespace,
# metallb_vip, sops_extract. Binding those into this module's globals with a
# `from probe_core import ...` would take a snapshot the patch never reaches.
# Everything else is imported by name, since nothing patches it.
import probe_core as core
from probe_core import (
    PI_HOST,
    curl_argv,
    k8s_endpoint,
    loki_labels_url,
    loki_query_url,
    pi_resolve,
    pi_url,
    prom_query_url,
    prom_targets_url,
    scrutiny_url,
    since_window_ns,
)

# The per-subcommand modules. Each `run_*` is re-exported here rather than dispatched
# through a registry, because `plan()` below is the one dispatch table and keeping the
# names in this module's namespace is what lets it stay a flat mapping.
from probe_alerts import run_alerts
from probe_arr import ARR_PORTS, run_arr
from probe_ha import run_ha, run_ha_state
from probe_health import (
    inspect_argv,
    k8s_deploy_argv,
    k8s_pods_argv,
    resolve_ip,
    run_health,
    run_readonly_rbac,
)
from probe_metrics import run_query
from probe_monitors import run_kuma_drift, run_monitors
from probe_releases import run_releases
from probe_storage import (
    LONGHORN_PREFIX,
    run_b2_budget,
    run_b2_longhorn,
    run_b2_record,
    run_b2_spend,
    run_longhorn_blocks,
)


def cert_stages(host, port, sni):
    """Two-stage pipeline: open a TLS session (with SNI) and decode the served
    leaf cert's subject/issuer/validity. Read-only — no data is sent.

    NB: connects to whatever DNS resolves `host` to. For a Cloudflare-proxied public host that's
    the CF edge (→ the Cloudflare edge cert), NOT Traefik's origin cert — pass the origin IP as the
    target with `--sni <host>` to inspect the origin Let's Encrypt cert."""
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
    sub = p.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("metric", help="Prometheus instant query")
    m.add_argument("promql")
    m.add_argument(
        "--json",
        action="store_true",
        help="print raw JSON instead of the formatted view",
    )
    sub.add_parser("targets", help="Prometheus scrape-target health")
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
        "--gap-min",
        type=float,
        default=30,
        help="minutes of silence that splits one episode from the next (default 30)",
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
    pi = sub.add_parser("pi", help="Pi glances API")
    pi.add_argument("subpath", help="e.g. fs, quicklook, mem, cpu")
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
        help="assert every automation in automations.yaml loaded (exit 0 = all loaded)",
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
    return p


def plan(args, resolve_ip, k8s_endpoint=k8s_endpoint, pi_resolve=pi_resolve):
    """Return the command pipeline (list of argv stages) for the parsed args.

    `resolve_ip(container) -> ip`, `k8s_endpoint(hostname) -> (base, pin)`, and
    `pi_resolve() -> pin` are injected so all routing/URL logic is testable without
    Docker, SOPS, or the network. Most commands are a single stage; `cert` is a
    two-stage openssl pipeline.
    """
    ns = _build_parser().parse_args(args)
    cmd = ns.cmd
    if cmd == "metric":
        base, pin = k8s_endpoint("prometheus")
        return [curl_argv(prom_query_url(base, ns.promql), resolve=pin)]
    if cmd == "targets":
        base, pin = k8s_endpoint("prometheus")
        return [curl_argv(prom_targets_url(base), resolve=pin)]
    if cmd == "loki-labels":
        base, pin = k8s_endpoint("loki-homelab")
        return [curl_argv(loki_labels_url(base), resolve=pin)]
    if cmd == "loki-query":
        base, pin = k8s_endpoint("loki-homelab")
        start, end = since_window_ns(getattr(ns, "since", None))
        return [
            curl_argv(
                loki_query_url(base, ns.logql, ns.limit, start=start, end=end),
                resolve=pin,
            )
        ]
    if cmd == "scrutiny":
        base, pin = k8s_endpoint("scrutiny")
        return [curl_argv(scrutiny_url(base), resolve=pin)]
    if cmd == "pi":
        # daniel-pi.lan is a Pi-hole-only LAN name; this host's resolver bypasses it (same
        # trap as every other cluster/LAN name here), so pin it like k8s_endpoint does.
        return [curl_argv(pi_url(ns.subpath), resolve=pi_resolve())]
    if cmd == "cert":
        host, _, port = ns.target.partition(":")
        port = int(port) if port else 443
        return cert_stages(host, port, ns.sni or host)
    raise SystemExit(f"unknown command: {cmd}")  # pragma: no cover


def run_pipeline(stages):
    """Run argv stages piped together (stdin of the first is closed)."""
    prev = subprocess.DEVNULL
    procs = []
    for i, stage in enumerate(stages):
        last = i == len(stages) - 1
        proc = subprocess.Popen(
            stage,
            stdin=prev,
            stdout=None if last else subprocess.PIPE,
            stderr=subprocess.DEVNULL if not last else None,
        )
        if prev not in (subprocess.DEVNULL, None):
            prev.close()
        prev = proc.stdout
        procs.append(proc)
    return procs[-1].wait()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    ns = _build_parser().parse_args(argv)
    # `health` parses/formats docker inspect rather than streaming a pipeline.
    if ns.cmd == "health":
        if ns.dry_run:
            if ns.docker:
                print(" ".join(["ssh", PI_HOST] + inspect_argv(ns.container)))
            else:
                ns_name = core.k8s_namespace()
                print(" ".join(k8s_deploy_argv(ns.container, ns_name)))
                print(
                    " ".join(k8s_deploy_argv(ns.container, ns_name, kind="daemonset"))
                    + "   # only if the Deployment lookup misses"
                )
                print(" ".join(k8s_pods_argv(ns.container, ns_name)))
            return 0
        return run_health(ns.container, docker=ns.docker)
    # Subcommands that answer from an API rather than streaming a shell pipeline. Each one is
    # `run_X(ns) -> int`, so the table is the whole dispatch — adding a subcommand is a parser
    # entry plus a row here. Built inside main() deliberately: run_b2_longhorn and its B2/Longhorn
    # siblings are defined BELOW main(), so a module-level table would name them before they exist.
    handlers = {
        # `ha` resolves a token + talks to the HA REST API.
        "ha": run_ha,
        "arr": run_arr,
        "alerts": run_alerts,
        "b2-longhorn": run_b2_longhorn,
        "b2-budget": run_b2_budget,
        "b2-spend": run_b2_spend,
        "longhorn-blocks": run_longhorn_blocks,
        "readonly-rbac": run_readonly_rbac,
        "b2-record": run_b2_record,
        "ha-state": run_ha_state,
        "monitors": run_monitors,
        "kuma-drift": run_kuma_drift,
        "releases": run_releases,
    }
    if ns.cmd in handlers:
        return handlers[ns.cmd](ns)
    # metric / loki-query default to a formatted view; --json and --dry-run fall
    # through to the raw streaming path below.
    if ns.cmd in ("metric", "loki-query") and not ns.json and not ns.dry_run:
        return run_query(ns)
    stages = plan(argv, resolve_ip)
    if ns.dry_run:
        for stage in stages:
            print(" ".join(stage))
        return 0
    return run_pipeline(stages)


if __name__ == "__main__":
    sys.exit(main())
