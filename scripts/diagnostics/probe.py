#!/usr/bin/env python3
"""Read-only homelab diagnostics.

One allow-listed surface for the queries that would otherwise be hand-written `curl`/`openssl`
one-offs.

The monitoring stack runs in k3s and publishes no host ports, so a hand-written `curl` needs an
address that outlives a redeploy. This wrapper derives each address at run time, which keeps a
single allow-list entry valid for every subcommand:

    Bash(uv run python scripts/diagnostics/probe.py:*)

It reaches a service by one of four transports:

- The cluster route `<name>.local.<domain>`, pinned to the MetalLB ingress VIP with
  `curl --resolve` (`probe_lib/core.py:k8s_endpoint`). Prometheus, the homelab Loki, Scrutiny,
  uptime-kuma and Home Assistant go this way, because this host's resolver bypasses the LAN
  DNS.
- The observability Loki's ClusterIP, read from the role default that pins it
  (`core.observability_loki_ip`). That Loki has no route.
- A k8s Service ClusterIP looked up through kubectl (`probe_lib/health_docker.py:
  resolve_service_ip`), for `arr`, whose API path has no Authelia bypass on the route.
- `ssh daniel-pi docker inspect`, for `health --docker` and `pi containers`. daniel-pi is the
  only host that still runs Docker.

Everything it runs is read-only (HTTP GET / TLS handshake / kubectl get / docker inspect).

Subcommands:
    metric '<promql>'        Prometheus instant query [--json] (prometheus :9090)
    targets                  Prometheus scrape-target health (prometheus :9090)
    monitors                 Kuma down-monitors rollup (exit 0 = all up), read from the
                             monitor_status metric Prometheus already scrapes — no Kuma
                             API credential needed
    loki-labels              Loki label names [--loki homelab|observability]
    loki-query '<logql>'     Loki range query [--limit N] [--json] [--loki homelab|observability]
                             Two Lokis: `homelab` (the default; pod, syslog and
                             monitor-bridge logs) and `observability` (Claude Code OTEL —
                             the only home of `{service_name="claude-code"}`; that
                             selector routes there on its own, and is refused at an
                             explicit --loki homelab rather than answered empty)
    alerts                   monitor-bridge DOWN history as episodes [--days N --check X --raw --json]
    scrutiny                 Disk SMART summary              (cluster scrutiny, both nodes)
    pi containers            every Pi container: state, health, networks (one ssh)
    cert <host[:port]>       Served TLS cert subj/dates [--sni NAME]
    shed-set                 untiered k8s workloads to scale down when one node is lost,
                             derived from the containers_list `tier` field (no kubectl)
    health <service>         k8s rollout + recent-restart rollup (exit 0 = healthy)
                             [--docker inspects the Pi's container instead]
                             [--cluster prod|stage — refuses when the local
                              kubectl serves the other one]
    arr <app> <api-path>     Read-only *arr API GET [--json] (sonarr/radarr/prowlarr)
                             [--show-secrets prints the credential values it redacts]
    ha state <entity_id>     Live HA entity state + attrs    (home-assistant :8123)
    ha automation <id|alias> One automation's on/off + last_triggered (resolves alias!=id)
    ha get <api-path>        Raw GET /api/<path>, e.g. `ha get error_log`
    ha trace <id|alias>      Why an automation last ran/no-op'd (per-condition WS trace; alias: why)
    ha verify-automations    Assert every automation in files/automations/ loaded (exit 0 = all loaded)
    ha verify-entities       Assert every entity in external_entities.yml still exists live
    ha-state [--inventory]   Live view of the derived HA state model
    landing [--json]         What would stop a landing now, runs in flight, worktrees + claims
    gitops-state [--json]    Every deployer marker + owed-ledger line with its clear command

Exit codes: every verdict subcommand exits 0 or 1 for what it observed, and 75 when it could
not read its source, with the failed read on stderr. Its last stdout line is
`PROBE-VERDICT: <ok|fail|unreadable> (<detail>)`, except under `--json` or `--dry-run`.
`lib/exit_codes.py`'s `CONTRACTS` holds the codes, and `--help` renders them.

`metric` and `loki-query` print a formatted view by default (one `<labels> = <value>`
line per series; log lines oldest→newest) so you don't need to pipe into `python3 -c`
to reshape the JSON — pass `--json` for the raw response.

`ha` is read-only (GET) and authenticates with the SOPS-encrypted claude_ha_token
(server-only — needs the host age key). The token is fed to curl via stdin, never argv.
`arr` works the same way — it pulls `<app>_api_key` from SOPS and passes it via stdin,
so the *arr key never lands in argv / `ps` / shell history. It also REDACTS the
credentials in its own output: `notification`, `downloadclient`, `indexer` and
`importlist` return objects whose `fields[].value` carry a Discord webhook URL, the
qBittorrent password or an indexer API key, and printing them puts a live credential in
the transcript. Pass `--show-secrets` when the value itself is what you need.
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

from diagnostics.probe_lib.cli_parser import ENTRY_POINT, _build_parser
from diagnostics.probe_lib.curl_pipeline import plan, stream_pipeline
from diagnostics.probe_lib.health import (
    inspect_argv,
    k8s_deploy_args,
    k8s_pods_args,
)

# Every subcommand's `run_*` callable comes from here rather than from its module: `main()`
# builds its `handlers` table from the entries flagged "handler", and reaches the four
# `subcommands.ROUTED_IN_MAIN` ones by name.
from diagnostics.probe_lib.subcommands import REGISTRY, VERDICT_HA_SUBCOMMANDS

from lib.exit_codes import (
    PROBE_FAILED,
    PROBE_UNREADABLE,
    PROBE_USAGE_ERROR,
    verdict_line,
)
from lib.kubectl import WrongCluster, kubectl_argv, nodes_args


def verdict_path(ns):
    """The subcommand path a `PROBE-VERDICT:` line names for `ns`, or None for no line.

    None for a subcommand that judges nothing, and for `--dry-run` and `--json`, whose stdout
    is a command list or one JSON document that a trailing line would break (the deck mod
    parses `landing --json`).
    """
    if getattr(ns, "dry_run", False) or getattr(ns, "json", False):
        return None
    if "verdict" not in REGISTRY.get(ns.cmd).flags:
        return None
    if ns.cmd == "targets":
        return "targets --pi" if ns.pi else None
    if ns.cmd == "ha":
        return f"ha {ns.ha_cmd}" if ns.ha_cmd in VERDICT_HA_SUBCOMMANDS else None
    return ns.cmd


def main(argv=None, *, plan=plan):
    """Parse argv, run the matching subcommand, and return its exit code.

    A verdict subcommand's output ends with one `PROBE-VERDICT: <ok|fail|unreadable> (...)`
    line, rendered from the exit code by `lib.exit_codes.verdict_line` so every subcommand
    shares one vocabulary (#4454). A source the subcommand could not read raises
    `core.Unreadable`, which exits `PROBE_UNREADABLE` (75) with the failure on stderr (#4444).
    An argument it cannot act on raises `core.UsageError`, which exits 64 with no verdict
    line, and any other message-carrying exit is the `fail` verdict (#4471).
    The line is printed after the subcommand returns, so `verdict_line` is the only place a
    code becomes a verdict word.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    # Handled on raw argv, ahead of `_build_parser().parse_args`: the subparsers below are
    # `required=True`, so `probe.py --list` alone would otherwise fail argparse's "cmd is
    # required" check before `ns.list` was ever read.
    if "--list" in argv:
        print("\n".join(REGISTRY.render_list()))
        return 0
    ns = _build_parser().parse_args(argv)
    path = verdict_path(ns)
    try:
        rc = _dispatch(ns, argv, plan)
    except core.Unreadable:
        # The exception printed its message to stderr when it was built.
        rc = PROBE_UNREADABLE
    except core.UsageError:
        # Printed on build, like Unreadable. 64 names no verdict, so no line follows it.
        rc = PROBE_USAGE_ERROR
    except SystemExit as exc:
        # A message-carrying SystemExit -- a group_vars key missing, an automation not found
        # -- is not a failed read: retrying it never helps, so it must not exit 75 (#4471).
        # A failed read raises `core.Unreadable` at its own site instead. A verdict subcommand
        # reports the rest as the `fail` verdict, still with its line, rather than exiting 1
        # with none.
        if path is None or isinstance(exc.code, int) or exc.code is None:
            raise
        print(exc.code, file=sys.stderr)
        rc = PROBE_FAILED
    if path:
        # The detail never repeats the failure: stderr carries it, and stdout stays the
        # verdict alone for a caller that reads only the last line.
        line = verdict_line(ENTRY_POINT, rc, f"probe.py {path}, exit {rc}")
        if line:
            print(line)
    return rc


def _dispatch(ns, argv, plan):
    """Run the subcommand `ns` names and return its exit code.

    `health` and the handler-table subcommands answer directly from an API or from
    `docker inspect`/kubectl. `metric`/`loki-query` without `--json`/`--dry-run` use the
    formatted view; every other subcommand falls through to the streaming `curl` pipeline
    built by `plan()`. `targets --pi` is checked ahead of that fallback, because plain
    `targets` still streams.

    `plan` is injectable so a test can see which invocations reach the streaming path without
    SOPS or the network.
    """
    # `health` parses/formats docker inspect rather than streaming a pipeline.
    if ns.cmd == "health":
        if ns.dry_run:
            if ns.docker:
                print(" ".join(["ssh", PI_HOST] + inspect_argv(ns.container)))
            else:
                ns_name = core.k8s_namespace()
                # First, because it is what decides whether the rest runs at all.
                print(
                    " ".join(kubectl_argv(*nodes_args()))
                    + f"   # refuses unless this serves the {ns.cluster} cluster"
                )
                print(" ".join(kubectl_argv(*k8s_deploy_args(ns.container, ns_name))))
                print(
                    " ".join(
                        kubectl_argv(
                            *k8s_deploy_args(ns.container, ns_name, kind="daemonset")
                        )
                    )
                    + "   # only if the Deployment lookup misses"
                )
                print(" ".join(kubectl_argv(*k8s_pods_args(ns.container, ns_name))))
            return 0
        run_health = REGISTRY.get("health").func
        return run_health(ns.container, docker=ns.docker, cluster=ns.cluster)
    if ns.cmd == "targets" and ns.pi:
        return REGISTRY.get("targets").func(ns)
    # Subcommands that answer from an API rather than streaming a shell pipeline. Each one is
    # `run_X(ns) -> int`, so the table is the whole dispatch: adding one is a row in
    # `probe_lib/subcommands.py` plus its parser entry.
    handlers = {e.name: e.func for e in REGISTRY if "handler" in e.flags}
    if ns.cmd in handlers:
        try:
            return handlers[ns.cmd](ns)
        except WrongCluster as exc:
            # Every kubectl read names the cluster it is about (`--cluster`, default prod) and
            # lib.kubectl refuses before running when the local kubectl serves another one.
            # One line, not a traceback: the refusal IS the answer.
            print(f"{ns.cmd}: {exc}")
            return 1
    # metric / loki-query default to a formatted view; --json and --dry-run fall
    # through to the raw streaming path below.
    if ns.cmd in ("metric", "loki-query") and not ns.json and not ns.dry_run:
        return REGISTRY.get(ns.cmd).func(ns)
    stages = plan(argv)
    if ns.dry_run:
        for stage in stages:
            print(" ".join(stage))
        return 0
    return stream_pipeline(stages)


if __name__ == "__main__":
    sys.exit(main())
