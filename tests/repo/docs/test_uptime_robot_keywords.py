"""Do the public endpoints still serve the keyword each Uptime Robot monitor is set to look for?

`docs/uptime-robot-monitors.md` is the only record of the monitors: they were created in the
console by hand. `probe.py uptime-robot` reads the console back; this test cannot.
What this test CAN check is the rule the console is meant to apply — that each documented
URL serves its documented keyword, case-sensitively, the way the monitor reads it. A body
that stops carrying the keyword pages Uptime Robot; a doc row that drifts from what the
endpoint serves is caught here first.

The rows are parsed from the doc's *What is configured* table by
`scripts/diagnostics/probe_lib/uptime_robot.py`, the parser `probe.py uptime-robot` uses to
check the same ids against the console. The doc is the oracle: changing a documented keyword
to a string the endpoint does not serve turns this red, and adding a monitor to the table
enrols it here without a code change.

**Marked `ui`, so CI never runs it.** It fetches the public hostnames over the real network,
which is what `-m ui` lifts the leakguard for. One GET per URL per run — bursting a public
name self-bans this host over IPv6 (`homelab-burst-tests-self-ban-over-ipv6`). Both
endpoints answered 200 in 0.31-0.38 s across three requests from daniel-box, so the 10 s
timeout is 25x the observed latency.

    uv run pytest -m ui -k uptime_robot
"""

import urllib.request

import pytest

from diagnostics.probe_lib.uptime_robot import documented_monitors

pytestmark = pytest.mark.ui

TIMEOUT_S = 10


def fetch(url: str) -> str:
    request = urllib.request.Request(
        url, headers={"User-Agent": "homelab-uptime-robot-keyword-test"}
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
        assert response.status == 200, f"{url}: HTTP {response.status}"
        return response.read().decode()


@pytest.mark.parametrize("monitor", documented_monitors(), ids=lambda m: m["name"])
def test_documented_keyword_rule_holds_on_the_live_endpoint(monitor):
    body = fetch(monitor["url"])
    present = monitor["keyword"] in body  # case-sensitive, as the console is set
    if monitor["rule"] == "exist":
        assert present, f"{monitor['url']} serves {body!r}, no {monitor['keyword']!r}"
    else:
        assert not present, f"{monitor['url']} serves {monitor['keyword']!r}: {body!r}"
