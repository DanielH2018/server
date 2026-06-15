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

    Bash(uv run python scripts/probe.py:*)

Everything it runs is read-only (HTTP GET / TLS handshake / docker inspect).

Subcommands:
    metric '<promql>'        Prometheus instant query        (prometheus :9090)
    targets                  Prometheus scrape-target health (prometheus :9090)
    loki-labels              Loki label names                (loki :3100)
    loki-query '<logql>'     Loki range query [--limit N]    (loki :3100)
    scrutiny                 Disk SMART summary              (scrutiny :8080)
    pi <subpath>             Pi glances API, e.g. `pi fs`    (daniel-pi.lan:61208)
    cert <host[:port]>       Served TLS cert subj/dates [--sni NAME]

Add `--dry-run` to print the command(s) instead of running them.
"""
import argparse
import subprocess
import sys
from urllib.parse import urlencode

DEFAULT_TIMEOUT = 10

# --- URL builders (pure) ----------------------------------------------------


def prom_query_url(ip, promql):
    return f"http://{ip}:9090/api/v1/query?" + urlencode({"query": promql})


def prom_targets_url(ip):
    return f"http://{ip}:9090/api/v1/targets"


def loki_labels_url(ip):
    return f"http://{ip}:3100/loki/api/v1/labels"


def loki_query_url(ip, logql, limit):
    return f"http://{ip}:3100/loki/api/v1/query_range?" + urlencode({"query": logql, "limit": limit})


def scrutiny_url(ip):
    return f"http://{ip}:8080/api/summary"


def pi_url(subpath):
    return f"http://daniel-pi.lan:61208/api/4/{subpath}"


# --- low-level argv / parsing helpers (pure) --------------------------------


def curl_argv(url, timeout=DEFAULT_TIMEOUT):
    return ["curl", "-sS", "--max-time", str(timeout), url]


def inspect_ip_argv(container):
    return ["docker", "inspect", "-f",
            "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}", container]


def parse_ip(inspect_output):
    """First non-empty token of `docker inspect`'s IP list (host can reach any
    of a container's bridge IPs). None if the container has no address."""
    for tok in inspect_output.split():
        if tok:
            return tok
    return None


def cert_stages(host, port, sni):
    """Two-stage pipeline: open a TLS session (with SNI) and decode the served
    leaf cert's subject/issuer/validity. Read-only — no data is sent."""
    s_client = ["openssl", "s_client", "-connect", f"{host}:{port}",
                "-servername", sni, "-verify_hostname", sni]
    x509 = ["openssl", "x509", "-noout", "-subject", "-issuer", "-dates",
            "-fingerprint", "-sha256"]
    return [s_client, x509]


# --- routing (pure given resolve_ip) ----------------------------------------


def _build_parser():
    p = argparse.ArgumentParser(prog="probe.py", description="read-only homelab diagnostics")
    p.add_argument("--dry-run", action="store_true", help="print the command(s) instead of running")
    sub = p.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("metric", help="Prometheus instant query")
    m.add_argument("promql")
    sub.add_parser("targets", help="Prometheus scrape-target health")
    sub.add_parser("loki-labels", help="Loki label names")
    lq = sub.add_parser("loki-query", help="Loki range query")
    lq.add_argument("logql")
    lq.add_argument("--limit", type=int, default=100)
    sub.add_parser("scrutiny", help="disk SMART summary")
    pi = sub.add_parser("pi", help="Pi glances API")
    pi.add_argument("subpath", help="e.g. fs, quicklook, mem, cpu")
    ct = sub.add_parser("cert", help="served TLS cert details")
    ct.add_argument("target", help="host or host:port")
    ct.add_argument("--sni", help="SNI servername (defaults to host)")
    return p


def plan(args, resolve_ip):
    """Return the command pipeline (list of argv stages) for the parsed args.

    `resolve_ip(container) -> ip` is injected so all routing/URL logic is testable
    without Docker or the network. Most commands are a single stage; `cert` is a
    two-stage openssl pipeline.
    """
    ns = _build_parser().parse_args(args)
    cmd = ns.cmd
    if cmd == "metric":
        return [curl_argv(prom_query_url(resolve_ip("prometheus"), ns.promql))]
    if cmd == "targets":
        return [curl_argv(prom_targets_url(resolve_ip("prometheus")))]
    if cmd == "loki-labels":
        return [curl_argv(loki_labels_url(resolve_ip("loki")))]
    if cmd == "loki-query":
        return [curl_argv(loki_query_url(resolve_ip("loki"), ns.logql, ns.limit))]
    if cmd == "scrutiny":
        return [curl_argv(scrutiny_url(resolve_ip("scrutiny")))]
    if cmd == "pi":
        return [curl_argv(pi_url(ns.subpath))]
    if cmd == "cert":
        host, _, port = ns.target.partition(":")
        port = int(port) if port else 443
        return cert_stages(host, port, ns.sni or host)
    raise SystemExit(f"unknown command: {cmd}")  # pragma: no cover


# --- runtime (impure) -------------------------------------------------------


def resolve_ip(container):
    out = subprocess.run(inspect_ip_argv(container), capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"docker inspect {container} failed: {out.stderr.strip()}")
    ip = parse_ip(out.stdout)
    if not ip:
        raise SystemExit(f"{container} has no container IP (is it running?)")
    return ip


def run_pipeline(stages):
    """Run argv stages piped together (stdin of the first is closed)."""
    prev = subprocess.DEVNULL
    procs = []
    for i, stage in enumerate(stages):
        last = i == len(stages) - 1
        proc = subprocess.Popen(
            stage, stdin=prev,
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
    dry = "--dry-run" in argv
    stages = plan(argv, resolve_ip)
    if dry:
        for stage in stages:
            print(" ".join(stage))
        return 0
    return run_pipeline(stages)


if __name__ == "__main__":
    sys.exit(main())
