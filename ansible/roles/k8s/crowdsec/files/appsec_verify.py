#!/usr/bin/env python3
"""Check that the CrowdSec engine has the AppSec WAF loaded, the edge's only enforcing signal.

Traefik's bouncer fails OPEN (`crowdsecAppsecUnreachableBlock: false`), so a broken AppSec
engine, such as a bad `cscli collections upgrade` or a hub rename that drops the AppSec rulesets,
leaves the edge on the ban list alone. The crowdsec pod stays up and LAPI-healthy while it does,
so nothing else reports it. This check asserts at least one enabled appsec-config and at least
one enabled appsec-rule. Both are traffic-independent: the request counters read zero on a quiet
homelab.

A total crowdsec outage belongs to the monitors that already own it, the cluster scrape job and
k3s Workload Health. When `cscli lapi status` fails, this check exits 0 with a message saying it
deferred, so the tile's heartbeat survives an engine restart without paging the same cause three
times.

It was `crowdsec-appsec-verify.sh`, a daniel-box root cron that ran `cscli` through `k3s kubectl
exec`, until H2.4b. It runs every 15 minutes in the `crowdsec-jobs` sidecar of the crowdsec pod,
started by `crowdsec_jobs.py`, which pushes the "CrowdSec AppSec" Kuma monitor from this
module's exit code and last stdout line.

Usage:

    python3 /jobs/appsec_verify.py

Takes no arguments and reads no environment variables. It runs the sidecar's own copy of
`cscli`, which reads the hub state from the shared `/etc/crowdsec` volume. Exit 0 means the WAF
is loaded or the check deferred to the outage monitors. Exit 1 means it is not loaded or a cscli
query failed while the LAPI answered, and the last stdout line says which. `-h` and `--help`
print this text and exit 0.
"""

import json
import subprocess
import sys

# The sidecar's initContainer copies the binary into the shared `tools` emptyDir
# (`templates/deployment.yaml.j2`).
CSCLI = "/tools/cscli"


def run(argv: list[str]) -> tuple[int, str]:
    done = subprocess.run(argv, capture_output=True, text=True)
    return done.returncode, done.stdout


def enabled_count(body: str, key: str) -> int | None:
    """How many items under `key` in a `cscli <kind> list -o json` body are enabled.

    None when the body is not that shape, which the caller reports as a query error rather than
    as zero items: zero is a real verdict and must not be reachable by a parse failure.
    """
    try:
        items = json.loads(body)[key]
    except ValueError, KeyError, TypeError:
        return None
    if not isinstance(items, list):
        return None
    return sum(
        1
        for item in items
        if isinstance(item, dict) and item.get("status") == "enabled"
    )


def verdict(runner=run) -> tuple[int, str]:
    """`(exit code, message)` for one check, from the cscli calls `runner` answers."""
    rc, _ = runner([CSCLI, "lapi", "status"])
    if rc != 0:
        return 0, "crowdsec unreachable — deferring to the scrape job + workload health"
    counts = {}
    for kind, empty in (
        (
            "appsec-configs",
            "no enabled appsec-configs — inline WAF not loaded, edge is ban-list-only",
        ),
        (
            "appsec-rules",
            "0 enabled appsec-rules loaded — WAF config present but not enforcing",
        ),
    ):
        rc, body = runner([CSCLI, kind, "list", "-o", "json"])
        count = enabled_count(body, kind) if rc == 0 else None
        if count is None:
            return 1, f"cscli {kind} list failed though LAPI is up (appsec query error)"
        if count < 1:
            return 1, empty
        counts[kind] = count
    return 0, (
        f"{counts['appsec-configs']} appsec-configs enabled, "
        f"{counts['appsec-rules']} enabled rules loaded"
    )


def main(runner=run) -> int:
    code, message = verdict(runner)
    print(message)
    return code


if __name__ == "__main__":
    if any(arg in ("-h", "--help") for arg in sys.argv[1:]):
        print(__doc__.strip())
        sys.exit(0)
    sys.exit(main())
