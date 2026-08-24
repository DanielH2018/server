#!/usr/bin/env python3
"""Measure real downtime across a rollout by polling a service while it restarts.

Every zero-downtime claim in docs/archive/zero-downtime-deploys-design.md is graded by this and
not by a manifest: `strategy: RollingUpdate` in a template says what was configured, and
this says what happened. A single-replica RollingUpdate only avoids a gap because maxSurge
rounds up to 1 — that is a scheduler behaviour, not a guarantee, and it fails silently if
the pod cannot be scheduled or the readiness probe is wrong.

Read-only: it issues GETs (or DNS queries) and never touches cluster state, so it runs fine
under the homelab-readonly service account. Trigger the rollout separately — typically
`uv run ansible-playbook ansible/deploy.yml --tags <svc>` in another terminal — while this
is running.

Usage:
    uv run python scripts/measure_rollout_gap.py --url https://grafana.local.example --seconds 180
    uv run python scripts/measure_rollout_gap.py --dns homepage.local.example --server 10.0.0.243 --seconds 180

Exit code is 0 only when zero requests failed, so it is usable as a gate.
"""

from __future__ import annotations

import argparse
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

Sample = tuple[float, bool]


@dataclass(frozen=True)
class GapReport:
    total: int
    failures: int
    longest_gap_s: float
    gaps: list[tuple[float, float]] = field(default_factory=list)


def summarize(samples: list[Sample]) -> GapReport:
    """Collapse samples into failure windows.

    A window runs from the first failed sample to the first success after it. A run of
    failures that never recovers is bounded by the last sample instead, so a service that
    stays down reads as a long gap rather than as no gap at all.

    This measures at poll resolution and therefore UNDERSTATES the true gap by up to one
    interval at each end — the service went down somewhere between the last success and the
    first failure. That is the right direction to be wrong in for a pass/fail gate (it never
    invents a gap), but do not quote `longest_gap_s` as an exact outage duration.
    """
    if not samples:
        return GapReport(total=0, failures=0, longest_gap_s=0.0, gaps=[])

    gaps: list[tuple[float, float]] = []
    start: float | None = None

    for ts, ok in samples:
        if not ok and start is None:
            start = ts
        elif ok and start is not None:
            gaps.append((start, ts))
            start = None

    if start is not None:
        gaps.append((start, samples[-1][0]))

    failures = sum(1 for _, ok in samples if not ok)
    longest = max((end - begin for begin, end in gaps), default=0.0)
    return GapReport(
        total=len(samples), failures=failures, longest_gap_s=longest, gaps=gaps
    )


def probe_http(url: str, timeout: float, insecure: bool) -> bool:
    ctx = ssl.create_default_context()
    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(url, timeout=timeout, context=ctx) as resp:
            return resp.status < 500
    except urllib.error.HTTPError as exc:
        # 4xx means something answered — an auth redirect or a 404 is not downtime.
        return exc.code < 500
    except OSError:
        return False


def probe_dns(name: str, server: str, timeout: float) -> bool:
    """Minimal A-record query. Avoids a dnspython dependency for one packet."""
    query = bytearray(b"\xab\xcd\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00")
    for label in name.rstrip(".").split("."):
        query.append(len(label))
        query.extend(label.encode())
    query.extend(b"\x00\x00\x01\x00\x01")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(bytes(query), (server, 53))
        data, _ = sock.recvfrom(512)
        return len(data) > 12 and (data[3] & 0x0F) == 0
    except OSError:
        return False
    finally:
        sock.close()


_READY_JSONPATH = (
    "{range .items[*]}{range .endpoints[*]}{.conditions.ready}{'\\n'}{end}{end}"
)


def ready_count(kubectl_stdout: str) -> int:
    """Ready endpoints in `kubectl get endpointslice -o jsonpath=<_READY_JSONPATH>` output.

    One `true`/`false` per line, with stray blanks between slices. Anything that is not
    exactly `true` counts as not ready, so a malformed line fails safe toward "no backend"
    rather than inventing one.
    """
    return sum(1 for line in kubectl_stdout.split("\n") if line.strip() == "true")


def probe_endpoints(service: str, namespace: str, timeout: float) -> tuple[bool, int]:
    """(has a ready backend, how many) for a Service, read from its EndpointSlices.

    For workloads a request loop cannot reach: a NetworkPolicy that refuses node-originated
    traffic, or no ingress route at all. `kubectl port-forward` is not a substitute — it dies
    with the pod it attached to, so it manufactures a gap at exactly the moment a rollout is
    worth measuring.

    Weaker than a request loop, and the caller must say so when reporting: this proves the
    Service had a routable backend, not that a request through it succeeded.
    """
    try:
        result = subprocess.run(
            [
                "kubectl",
                "-n",
                namespace,
                "get",
                "endpointslice",
                "-l",
                f"kubernetes.io/service-name={service}",
                "-o",
                f"jsonpath={_READY_JSONPATH}",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    # Two clauses rather than one tuple: `ruff format` strips the parentheses off
    # `except (A, B):`, and the unparenthesized form is a SyntaxError under the 3.12 that
    # `#!/usr/bin/env python3` resolves to on these hosts. See test_host_scripts_py312.py.
    except subprocess.SubprocessError:
        return False, -1
    except OSError:
        return False, -1

    if result.returncode != 0:
        return False, -1
    n = ready_count(result.stdout)
    return n >= 1, n


def run(args: argparse.Namespace) -> tuple[GapReport, int]:
    """(gap report, peak ready endpoints). Peak is -1 outside --endpoints mode."""
    samples: list[Sample] = []
    peak = -1
    started = time.monotonic()
    while time.monotonic() - started < args.seconds:
        at = time.monotonic() - started
        if args.endpoints:
            ok, n = probe_endpoints(args.endpoints, args.namespace, args.timeout)
            peak = max(peak, n)
            detail = f"ready={n}"
        elif args.dns:
            ok = probe_dns(args.dns, args.server, args.timeout)
            detail = "FAIL"
        else:
            ok = probe_http(args.url, args.timeout, args.insecure)
            detail = "FAIL"
        samples.append((at, ok))
        if not ok:
            print(f"  {at:7.2f}s  {detail}", flush=True)
        time.sleep(args.interval)
    return summarize(samples), peak


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--url", help="HTTP(S) URL to poll")
    target.add_argument("--dns", help="hostname to resolve instead of polling HTTP")
    target.add_argument(
        "--endpoints",
        help="Service name whose ready endpoints to count, for workloads a request loop "
        "cannot reach (NetworkPolicy-fenced, or no ingress route)",
    )
    parser.add_argument("--server", default="10.0.0.243", help="DNS server for --dns")
    parser.add_argument(
        "--namespace", default="homelab", help="namespace for --endpoints"
    )
    parser.add_argument("--seconds", type=float, default=180.0, help="how long to poll")
    parser.add_argument(
        "--interval", type=float, default=0.5, help="seconds between polls"
    )
    parser.add_argument(
        "--timeout", type=float, default=2.0, help="per-request timeout"
    )
    parser.add_argument("--insecure", action="store_true", help="skip TLS verification")
    args = parser.parse_args(argv)

    target_desc = args.endpoints or args.dns or args.url
    print(f"Polling {target_desc} every {args.interval}s for {args.seconds}s.")
    print("Trigger the rollout now, in another terminal.\n")

    report, peak = run(args)
    unit = "samples" if args.endpoints else "requests"

    print(f"\n{unit:11}: {report.total}")
    print(f"{'failures':11}: {report.failures}")
    print(f"{'longest gap':11}: {report.longest_gap_s:.2f}s")
    for begin, end in report.gaps:
        print(f"  gap {begin:7.2f}s -> {end:7.2f}s  ({end - begin:.2f}s)")

    if args.endpoints:
        print(f"{'peak ready':11}: {peak}")
        # Only a caveat on a pass. When samples failed, the low peak IS the finding, and
        # explaining it away as an EndpointSlice write would be actively misleading.
        if peak == 1 and not report.failures:
            print(
                "  NOTE: never saw two ready backends at once, so the surge was not "
                "observed —\n  the EndpointSlice probably swapped addresses in one update. "
                "Zero gaps here means\n  the Service always had a backend, NOT that an "
                "in-flight request survived."
            )
        elif peak <= 0:
            print(
                "  NOTE: never saw ANY ready backend. Check the service name and namespace "
                "before\n  reading this as an outage — a typo looks identical to a service "
                "that is down."
            )

    if report.total == 0:
        print(f"\nFAIL: no {unit} were made.")
        return 2
    if report.failures:
        print(f"\nFAIL: {report.failures} failed {unit}.")
        return 1
    print(f"\nPASS: zero failed {unit}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
