"""The B2 storage log line is a dashboard contract, not just a message.

B2 exposes no usage API, so `b2_storage`'s own log line is the ONLY record of stored bytes. The
"B2 (off-site) — storage runway" panels on the Longhorn — Storage board parse it with a LogQL
regexp. Rewording the message would leave those panels blank with no other signal — the same
shape as a guard that goes textually inert. These bind the two together.
"""

import json
import re
from pathlib import Path


import checks_b2

_REPO = Path(__file__).resolve().parents[5]


# ── the B2 storage line is a dashboard contract, not just a message ────────────────────────────
#
# B2 exposes no usage API, so `b2_storage`'s own log line is the ONLY record of stored bytes.
# The "B2 (off-site) — storage runway" panels on the Longhorn — Storage board parse it with a
# LogQL regexp. Rewording the message would leave those panels blank with no other signal —
# the same shape as a guard that goes textually inert. These bind the two together.

_TICK = chr(96)

_BOARD = (
    Path(__file__).resolve().parents[3]
    / "k8s"
    / "claude-otel"
    / "files"
    / "dashboards"
    / "Infrastructure"
    / "longhorn-storage.json"
)


def _dashboard_b2_regex() -> str:
    """The regexp the B2 runway panels parse the log line with, read from the board itself."""
    board = json.loads(_BOARD.read_text())
    for panel in board["panels"]:
        for target in panel.get("targets", []):
            expr = target.get("expr", "")
            if "B2 storage" in expr and "| regexp " in expr:
                return expr.split("| regexp " + _TICK, 1)[1].split(_TICK, 1)[0]
    raise AssertionError(
        "no B2 storage regexp panel found on the Longhorn — Storage board"
    )


def test_the_b2_storage_line_still_matches_the_dashboard_regex():
    """The rejecting half is the message drifting: this fails the moment the wording changes."""
    ok, msg = checks_b2.b2_storage_verdict(
        used_bytes=5_100_000_000,
        versions=1110,
        truncated=False,
        cap=10_000_000_000,
        max_pct=90,
    )
    assert ok
    match = re.search(_dashboard_b2_regex(), msg)
    assert match, (
        f"the b2_storage message no longer matches the dashboard regexp: {msg!r}"
    )
    assert match.group("used_gb") == "5.10"
    assert match.group("cap_gb") == "10"
    assert match.group("pct") == "51"
    assert match.group("versions") == "1110"


def test_the_dashboard_regex_rejects_a_reworded_line():
    """The accepting half's mirror: a regexp loose enough to match anything would pass the test
    above while pinning nothing."""
    assert not re.search(_dashboard_b2_regex(), "B2 storage is fine, 1110 objects")


def test_every_b2_runway_panel_collapses_its_series():
    """Each unwrapped capture leaves the others as stream labels, so `versions` ticking over
    spawns a new series and the panel draws a staircase of one-point lines. Measured live: the
    unwrapped form returned 2 series for one logical value. avg() is what makes it one."""
    board = json.loads(_BOARD.read_text())
    exprs = [
        t["expr"]
        for panel in board["panels"]
        for t in panel.get("targets", [])
        if "B2 storage" in t.get("expr", "")
    ]
    assert exprs, "the B2 runway panels are gone from the board"
    for expr in exprs:
        assert expr.startswith("avg(avg_over_time("), expr
