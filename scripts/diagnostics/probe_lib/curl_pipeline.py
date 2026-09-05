"""The streaming half of probe.py: argv -> curl/openssl stages -> a piped run.

Split out of probe.py, which had grown to 697 lines. `plan()` turns parsed arguments into the
command pipeline a streaming subcommand runs, and `stream_pipeline()` runs one. The
subcommands that answer from an API instead never reach here — `probe.py`'s `main()` dispatches
those through its own `handlers` table.

The runner is named `stream_pipeline`, not `run_pipeline` as it was in probe.py. Inside
`probe_lib/` a module-level `run_*` means "this module backs a subcommand":
`lib.registry.package_entry_points` collects those names, and
`scripts/diagnostics/tests/test_probe_registry.py` asserts the set is exactly the twelve
subcommand backends. Moving the function in under its old name would have made this module a
thirteenth. `main()`'s own docstring already called this path the streaming one.

This module imports `cli_parser.py`; `cli_parser.py` never imports this one.

The file is `curl_pipeline.py` rather than `pipeline.py` because
`scripts/deploy_tools/land_lib/pipeline.py` already holds that basename, and
`docs/reference/scripts.md` keys every row on the bare filename — see
`test_no_two_scripts_share_a_basename` in `scripts/docs/tests/test_gen_reference_scripts.py`.
"""

import subprocess

# `probe_lib` is a namespace package under `scripts/`, so reaching a sibling by package name
# needs `scripts/` on sys.path — a module gets only its importer's path otherwise, and
# pyproject's `pythonpath` is a pytest setting. This has to sit ABOVE the imports below.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from diagnostics.probe_lib.cli_parser import cert_stages, _build_parser
from diagnostics.probe_lib.core import (
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


def stream_pipeline(stages):
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
