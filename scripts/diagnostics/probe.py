#!/usr/bin/env python3
"""Read-only homelab diagnostics.

One allow-listed surface for the queries that used to be hand-written `curl`/`openssl`
one-offs.

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
    ha verify-automations    Assert every automation in files/automations/ loaded (exit 0 = all loaded)
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

import sys
from pathlib import Path as _Path

# The subcommand modules live in `probe_lib`, a namespace package under `scripts/`, so
# reaching them by package name needs `scripts/` on sys.path: a directly-invoked script —
# which is the ONLY way this file runs in production — gets just its own directory, and
# pyproject's `pythonpath` is a pytest setting. This has to sit ABOVE the imports below.
sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

# `core.<name>` for anything the tests monkeypatch — `k8s_namespace` is the one this file
# still reads. Binding it into this module's globals with a `from core import ...` would take
# a snapshot the patch never reaches.
from diagnostics.probe_lib import core
from diagnostics.probe_lib.core import PI_HOST

# The per-subcommand modules. Each `run_*` is imported here because the `handlers` table in
# `main()` is what actually dispatches to it. `subcommands.REGISTRY` carries the same set as
# metadata, for `--list` and for the completeness guard in
# `scripts/diagnostics/tests/test_probe_registry.py`; it does not replace `handlers`, so
# argparse and dispatch stay exactly as they were.
from diagnostics.probe_lib.alerts import run_alerts
from diagnostics.probe_lib.arr import run_arr
from diagnostics.probe_lib.b2_ledger import (
    run_b2_deletions,
    run_b2_record,
    run_b2_spend,
)
from diagnostics.probe_lib.cli_parser import _build_parser
from diagnostics.probe_lib.ha import run_ha, run_ha_state
from diagnostics.probe_lib.health import (
    inspect_argv,
    k8s_deploy_argv,
    k8s_pods_argv,
    resolve_ip,
    run_health,
)
from diagnostics.probe_lib.longhorn import (
    run_b2_budget,
    run_b2_longhorn,
    run_longhorn_blocks,
)
from diagnostics.probe_lib.metrics import run_query
from diagnostics.probe_lib.monitors import run_kuma_drift, run_monitors
from diagnostics.probe_lib.pi_plane import run_pi_containers, run_pi_targets
from diagnostics.probe_lib.curl_pipeline import plan, stream_pipeline
from diagnostics.probe_lib.readonly_rbac import run_readonly_rbac
from diagnostics.probe_lib.releases import run_releases
from diagnostics.probe_lib.subcommands import REGISTRY
from diagnostics.probe_lib.vip_placement import run_vip_placement


def main(argv=None):
    """Parse argv, dispatch to the matching subcommand, and return its exit code.

    `health` and the handler-table subcommands answer directly from an API or from
    `docker inspect`/kubectl. `metric`/`loki-query` without `--json`/`--dry-run` use the
    formatted view; every other subcommand falls through to the streaming `curl` pipeline
    built by `plan()`. `targets --pi` and `pi containers` are checked ahead of that fallback:
    plain `targets` and every other `pi <subpath>` still stream, so only the Pi-scoped variants
    need a real handler.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    # Handled on raw argv, ahead of `_build_parser().parse_args`: the subparsers below are
    # `required=True`, so `probe.py --list` alone would otherwise fail argparse's "cmd is
    # required" check before `ns.list` was ever read.
    if "--list" in argv:
        print("\n".join(REGISTRY.render_list()))
        return 0
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
    if ns.cmd == "targets" and ns.pi:
        return run_pi_targets(ns)
    if ns.cmd == "pi" and ns.subpath == "containers":
        return run_pi_containers(ns)
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
        "vip-placement": run_vip_placement,
        "b2-record": run_b2_record,
        "b2-deletions": run_b2_deletions,
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
    return stream_pipeline(stages)


if __name__ == "__main__":
    sys.exit(main())
