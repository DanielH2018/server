#!/usr/bin/env python3
"""Turn `probe.py loki-labels` output into an up/down verdict for the Loki read-route witness.

WHY THE EXIT CODE CANNOT BE THE VERDICT (issue #1712). `probe.py` builds its curl argv without
`-f` (`scripts/diagnostics/probe_lib/core.py`, `curl_argv`), so a request Traefik refuses returns
the body `404 page not found` and exits **0**. A check that pushed on the exit code alone would
have read UP through the entire window #1693 describes, when this route was dead from
daniel-server — green and inert, which is the failure this repo has paid for twice
(repo-root CLAUDE.md).

So the verdict is read from the BODY: valid JSON, `status: success`, and a non-empty `data`
array. Anything else is DOWN, including a body that parses but carries no label names — Loki
answering with an empty label set means the read path returns nothing useful, which is what the
operator's `loki-query` would hit next.

Deployed by roles/setup/initial_setup to /opt/loki-route-health/ and run by
/usr/local/bin/loki-read-route-health.sh. Stdlib only: the wrapper runs it through
`uv run --no-project` on the pinned interpreter, so it resolves no project and needs no
dependencies of its own.

Usage: <probe.py stdout on stdin> | loki_route_health.py <probe.py exit code>
Exits 0 for up, 1 for down, and prints the message the Kuma push carries.
"""

import json
import sys

# The message goes into a Kuma push (a URL query parameter) and a syslog line, so a whole 404
# page or a Python traceback is trimmed rather than carried.
_MAX_DETAIL = 160


def _detail(body: str) -> str:
    flat = " ".join(body.split())
    return flat[:_MAX_DETAIL] if flat else "(no output)"


def verdict(rc: int, body: str) -> tuple[str, str]:
    """Return (`up`|`down`, message) for one `probe.py loki-labels` run."""
    if rc != 0:
        return "down", f"probe.py loki-labels exited {rc}: {_detail(body)}"
    try:
        data = json.loads(body)
    except ValueError:
        # The dominant failure: Traefik refusing the route answers `404 page not found`, which
        # is not JSON and which probe.py reports with exit 0.
        return "down", f"the Loki read route did not answer with JSON: {_detail(body)}"
    if not isinstance(data, dict) or data.get("status") != "success":
        return "down", f"Loki answered without status=success: {_detail(body)}"
    labels = data.get("data")
    if not isinstance(labels, list) or not labels:
        return "down", "Loki answered success with no label names"
    return "up", f"the Loki read route returned {len(labels)} label names"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(
            "usage: loki_route_health.py <probe-exit-code> (body on stdin)",
            file=sys.stderr,
        )
        return 2
    status, msg = verdict(int(argv[1]), sys.stdin.read())
    print(msg)
    return 0 if status == "up" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
