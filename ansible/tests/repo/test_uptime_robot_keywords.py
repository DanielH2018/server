"""Do the public endpoints still serve the keyword each Uptime Robot monitor is set to look for?

`docs/uptime-robot-monitors.md` is the only record of the monitors: they were created in the
console by hand, no API key exists in the tree, and nothing here can read the console back.
What this test CAN check is the rule the console is meant to apply — that each documented
URL serves its documented keyword, case-sensitively, the way the monitor reads it. A body
that stops carrying the keyword pages Uptime Robot; a doc row that drifts from what the
endpoint serves is caught here first (issue #2168).

The rows are parsed from the doc's *What is configured* table, so the doc is the oracle:
changing a documented keyword to a string the endpoint does not serve turns this red, and
adding a monitor to the table enrols it here without a code change.

**Marked `ui`, so CI never runs it.** It fetches the public hostnames over the real network,
which is what `-m ui` lifts the leakguard for. One GET per URL per run — bursting a public
name self-bans this host over IPv6 (`homelab-burst-tests-self-ban-over-ipv6`). Measured
2026-09-21 from daniel-box: both endpoints answered 200 in 0.31-0.38 s across three
requests, so the 10 s timeout is 25x the observed latency.

    uv run pytest -m ui -k uptime_robot
"""

import re
import urllib.request
from _helpers import REPO

import pytest

pytestmark = pytest.mark.ui

DOC = REPO / "docs" / "uptime-robot-monitors.md"

# One row of the *What is configured* table: `| name | `id` | `url` | `keyword` must exist |`.
_ROW = re.compile(
    r"^\| (?P<name>[^|]+?) \| `(?P<id>\d+)` \| `(?P<url>https://[^`]+)` \|"
    r" `(?P<keyword>[^`]+)` must (?P<rule>exist|not exist) \|$",
    re.MULTILINE,
)

# The two monitors live since 2026-08-30. The parse is a regex over prose, so a reworded table
# would otherwise yield an empty set and a vacuously green parametrize.
KNOWN_MONITORS = frozenset({"Auth Health", "Jellyfin Health"})

TIMEOUT_S = 10


def documented_monitors() -> list[dict[str, str]]:
    rows = [m.groupdict() for m in _ROW.finditer(DOC.read_text())]
    names = {row["name"] for row in rows}
    missing = KNOWN_MONITORS - names
    assert not missing, f"{DOC.name}: monitor table no longer lists {sorted(missing)}"
    return rows


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
