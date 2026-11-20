"""The poller routes no fall of a service the restart broker may restart.

Each test drives `main` through its seams, so it covers the config key as well as the rule.
"""

import json
from datetime import UTC, datetime

import claude_triage_poll as poll
import pytest

T0 = datetime(2026, 10, 15, 18, 30, tzinfo=UTC).timestamp()
ROUTES = '{"CrashLooping": "triage", "CrashLooping:docs": "triage"}'


def _config(tmp_path, extra: str) -> str:
    (tmp_path / "state" / "inbox").mkdir(parents=True)
    path = tmp_path / "poll.cfg"
    path.write_text(
        f"STATE_DIR={tmp_path / 'state'}\nPOLL_MODE=armed\nPROM_URLS=http://loopback\n"
        f"POLL_STREAK=1\nPOLL_ROUTES={ROUTES}\n" + extra
    )
    return str(path)


def _poll(config: str, alerts: list[dict]) -> int:
    def read(base, query, refusal_expected=False):
        return alerts if query == poll.ALERTS_QUERY else [{"value": [T0, "86400"]}]

    return poll.main(
        ["claude_triage_poll.py"], config, read, lambda episode: None, lambda: T0
    )


def _queued_services(tmp_path, restart_services: str) -> set[str]:
    """The services whose crash loop queues an episode under `RESTART_SERVICES`."""
    config = _config(tmp_path, f"RESTART_SERVICES={restart_services}\n")
    falls = [
        {"metric": {"alertname": "CrashLooping", "service": s}, "value": [T0, "1"]}
        for s in ("docs", "sonarr", "unmapped")
    ]
    assert _poll(config, []) == 0  # the baseline
    assert _poll(config, falls) == 0
    inbox = tmp_path / "state" / "inbox"
    return {json.loads(p.read_text())["service"] for p in inbox.iterdir()}


def test_a_fall_of_a_restartable_service_opens_nothing_whatever_the_table_says(
    tmp_path,
) -> None:
    """An unmapped fall may be the restarted workload's, so it opens nothing either."""
    assert _queued_services(tmp_path / "listed", '["docs"]') == {"sonarr"}
    assert _queued_services(tmp_path / "empty", "[]") == {"docs", "sonarr", "unmapped"}


@pytest.mark.parametrize("extra", ["RESTART_SERVICES=docs\n", ""], ids=["bad", "gone"])
def test_a_poll_that_cannot_read_the_restartable_services_fails(
    tmp_path, extra
) -> None:
    """An empty set would route a listed service again, so the poll stops and pages."""
    with pytest.raises(ValueError, match="RESTART_SERVICES"):
        _poll(_config(tmp_path, extra), [])
