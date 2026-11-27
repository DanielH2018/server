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
  `curl --resolve` (`probe_lib/core.py:k8s_endpoint`). Prometheus, the homelab Loki, Scrutiny
  and Home Assistant go this way, because this host's resolver bypasses the LAN
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
    monitors                 firing alerts from ALERTS and Alertmanager by severity and
                             service, and active silences (exit 0 = nothing
                             paging), read through Prometheus and Alertmanager
    loki-labels              Loki label names [--loki homelab|observability]
    loki-query '<logql>'     Loki range query [--limit N] [--since 6h] [--until 1h]
                             [--step 5m] [--timeout 60] [--json]
                             [--loki homelab|observability]
                             Two Lokis: `homelab` (the default; pod, syslog and
                             monitor-bridge logs) and `observability` (Claude Code OTEL —
                             the only home of `{service_name="claude-code"}`; that
                             selector routes there on its own, and is refused at an
                             explicit --loki homelab rather than answered empty)
    alerts                   ALERTS firing history as episodes [--days N --check X --pi --json]
    scrutiny                 Disk SMART summary              (cluster scrutiny, both nodes)
    pi containers            every Pi container: state, health, networks (one ssh)
    cert <host[:port]>       Served TLS cert subj/dates [--sni NAME]
    shed-set                 untiered k8s workloads to scale down when one node is lost,
                             derived from the containers_list `tier` field (no kubectl)
    flux [tag...]            Flux Kustomizations: Ready + reason, applied commit vs the
                             source's, suspended; a tag adds its -verify etc. and soak
    takeover <tag>           field paths Flux's first apply of the tag strips (paths only;
                             exit 75 when a read fails or nothing rendered is live)
    health <service>         k8s rollout + recent-restart rollup (exit 0 = healthy)
                             [--docker inspects the Pi's container instead]
                             [--cluster prod|stage — refuses when the local
                              kubectl serves the other one]
    arr <app> <api-path>     Read-only *arr API GET [--json] (sonarr/radarr/prowlarr/lidarr)
                             [--show-secrets prints the credential values it redacts]
    uptime-robot             every Uptime Robot monitor the docs name exists and is not
                             paused (key from SOPS uptimerobot_api_key, via stdin)
    ha state <entity_id>     Live HA entity state + attrs    (home-assistant :8123)
    ha automation <id|alias> One automation's on/off + last_triggered (resolves alias!=id)
    ha get <api-path>        Raw GET /api/<path>, e.g. `ha get error_log`
    ha trace <id|alias>      Why an automation last ran/no-op'd (per-condition WS trace; alias: why)
    ha verify-automations    Assert every automation in files/automations/ loaded (exit 0 = all loaded)
    ha verify-entities       Assert every entity in external_entities.yml still exists live
    ha-state [--inventory]   Live view of the derived HA state model
    landing [--json]         What would stop a landing now, runs in flight, worktrees + claims
    landing-verdicts [<pr>]  Newest recorded VERDICT line per landed PR, from both k3s hosts
                             [--json --limit N --all] (exit 1 when <pr> has none, 75 when
                             a host did not answer)
    push-rules <branch>      Which ruleset rule a push to the branch meets and whether the gh
                             login bypasses it [--rev HEAD] [--remote origin]
    lander [--pr N]          daniel-box's server-batch lander: newest pass, the landings it
                             started with their VERDICT, the PRs it named [--since 6h]
    builder [-n N]           daniel-box's host-package builder: reason, published versions,
                             newest builds, failures [--since 24h]
    usage [--all]            requests per service over the retention window (all routes
                             summed, monitoring apart), memory request/limit vs 7d p95,
                             CPU request/limit vs 7d p95 with throttling and PSI, PVC fill
                             and days to full (prometheus :9090)

Exit codes: every verdict subcommand exits 0 or 1 for what it observed, and 75 when it could
not read its source, with the failed read on stderr. `health` exits 3 when the tag names nothing
it can check. The last stdout line is `PROBE-VERDICT: <ok|fail|skip|unreadable> (<detail>)`,
except under `--json` or `--dry-run`.
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
observability's `CertExpiringSoon` rule).
"""

import shlex
import sys


# Use `core.<name>` for anything tests monkeypatch: a `from core import` binds a snapshot the patch
# never reaches.
from diagnostics.probe_lib import core
from diagnostics.probe_lib.cli_parser import ENTRY_POINT, _build_parser
from diagnostics.probe_lib.curl_pipeline import output_redaction, plan, stream_pipeline
from diagnostics.probe_lib.health import (
    inspect_argv,
    k8s_deploy_args,
    k8s_pods_args,
)

from diagnostics.probe_lib.subcommands import REGISTRY, VERDICT_HA_SUBCOMMANDS

from lib.exit_codes import (
    PROBE_FAILED,
    PROBE_UNREADABLE,
    PROBE_USAGE_ERROR,
)
from lib.exit_messages import verdict_line
from lib.kubectl import WrongCluster, kubectl_argv, nodes_args


def verdict_path(ns):
    """The subcommand path a `PROBE-VERDICT:` line names for `ns`, or None for no line.

    None for a subcommand that judges nothing, and for `--dry-run` and `--json`, whose stdout
    is a command list or one JSON document that a trailing line would break.
    """
    if getattr(ns, "dry_run", False) or getattr(ns, "json", False):
        return None
    if "verdict" not in REGISTRY.get(ns.cmd).flags:
        return None
    if ns.cmd == "targets":
        return "targets --pi" if ns.pi else "targets"
    if ns.cmd == "ha":
        return f"ha {ns.ha_cmd}" if ns.ha_cmd in VERDICT_HA_SUBCOMMANDS else None
    return ns.cmd


def main(argv=None, *, plan=plan):
    """Parse argv, run the matching subcommand, and return its exit code.

    A verdict subcommand's output ends with one `PROBE-VERDICT: <ok|fail|skip|unreadable> (...)`
    line, rendered from the exit code by `lib.exit_messages.verdict_line` so every subcommand
    shares one vocabulary. A source the subcommand could not read raises
    `core.Unreadable`, which exits `PROBE_UNREADABLE` (75) with the failure on stderr.
    An argument it cannot act on raises `core.UsageError`, which exits 64 with no verdict
    line, and any other message-carrying exit is the `fail` verdict.
    The line is printed after the subcommand returns, so `verdict_line` is the only place a
    code becomes a verdict word.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    # Handled on raw argv: the subparsers are `required=True`, so `--list` alone would fail argparse
    # first.
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
        # A message-carrying SystemExit is not a failed read: retrying never helps, so it must not
        # exit 75.
        if path is None or isinstance(exc.code, int) or exc.code is None:
            raise
        print(exc.code, file=sys.stderr)
        rc = PROBE_FAILED
    if path:
        # stderr carries the failure, so stdout stays the verdict alone for a caller reading the
        # last line.
        line = verdict_line(ENTRY_POINT, rc, f"probe.py {path}, exit {rc}")
        if line:
            print(line)
    return rc


def _routed(name, *args, **kwargs) -> int:
    """Run the callable registered for `name`, which must have one, and return its exit code."""
    func = REGISTRY.get(name).func
    if not callable(func):
        raise LookupError(f"probe.py subcommand {name!r} has no callable")
    rc = func(*args, **kwargs)
    if not isinstance(rc, int):
        raise TypeError(
            f"probe.py subcommand {name!r} returned {rc!r}, not an exit code"
        )
    return rc


def _dispatch(ns, argv, plan):
    """Run the subcommand `ns` names and return its exit code.

    `health` and the handler-table subcommands answer directly from an API or from
    `docker inspect`/kubectl. `metric`/`loki-query` without `--json`/`--dry-run` use the
    formatted view; every other subcommand falls through to the streaming `curl` pipeline
    built by `plan()`. `targets` is checked ahead of that fallback, because `targets --json`
    still streams.

    `plan` is injectable so a test can see which invocations reach the streaming path without
    SOPS or the network.
    """
    # `health` parses/formats docker inspect rather than streaming a pipeline.
    if ns.cmd == "health":
        if ns.dry_run:
            if ns.docker:
                print(
                    shlex.join(core.pi_ssh_argv(shlex.join(inspect_argv(ns.container))))
                )
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
        return _routed("health", ns.container, docker=ns.docker, cluster=ns.cluster)
    if ns.cmd == "targets" and (ns.pi or not (ns.json or ns.dry_run)):
        return _routed("targets", ns)
    handlers = {e.name for e in REGISTRY if "handler" in e.flags}
    if ns.cmd in handlers:
        try:
            return _routed(ns.cmd, ns)
        except WrongCluster as exc:
            # The local kubectl serves another cluster: nothing was read, so exit 75, never the
            # `fail`
            # verdict.
            print(f"{ns.cmd}: {exc}", file=sys.stderr)
            return PROBE_UNREADABLE
    if ns.cmd in ("metric", "loki-query") and not ns.json and not ns.dry_run:
        return _routed(ns.cmd, ns)
    stages = plan(argv)
    if ns.dry_run:
        for stage in stages:
            print(" ".join(stage))
        return 0
    return stream_pipeline(stages, redact=output_redaction(ns.cmd))


if __name__ == "__main__":
    sys.exit(main())
