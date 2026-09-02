"""End-to-end wiring for the pending-soak check: the clock's persistence and the digest.

The pure-function tests next door prove the verdict can go red. They cannot see the two ways
this check goes inert in the I/O shell, and both are the whole point of it:

  1. The clock must be written on runs that post NOTHING. The `last_notified` fingerprint
     beside it is deliberately gated on a confirmed Discord post; copying that idiom for the
     first-seen map would reset every dwell on each quiet day, and no item could ever reach
     its threshold.
  2. An aged clock must actually reach Discord.
"""

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "files"))
import renovate_notify as rn

DAY = 86400.0
DASHBOARD = {
    "title": "Dependency Dashboard",
    "user": {"login": "renovate[bot]"},
    "updated_at": "2999-01-01T00:00:00Z",  # never stale, so only the pending arm can fire
    "body": (
        "## Pending Status Checks\n\n"
        " - [ ] <!-- approvePr-branch=renovate/promtail -->"
        "Update grafana/promtail Docker tag to v3.6.11\n"
    ),
}


def _wire(monkeypatch, tmp_path, posts, now):
    """Point main() at a fake GitHub + Discord and a state dir under tmp_path."""
    monkeypatch.setattr(
        rn,
        "cfg",
        lambda: {
            "REPO": "o/r",
            "DISCORD_WEBHOOK": "https://discord.example/hook",
            "STATE_DIR": str(tmp_path),
        },
    )
    monkeypatch.setattr(rn, "github_token", lambda *_a, **_k: "")
    monkeypatch.setattr(rn, "get", lambda url: [DASHBOARD] if "/issues" in url else [])
    monkeypatch.setattr(
        rn, "discord", lambda hook, content: posts.append(content) or True
    )
    monkeypatch.setattr(rn.time, "time", lambda: now)


def test_clock_is_written_on_a_run_that_posts_nothing(monkeypatch, tmp_path):
    posts = []
    now = 1_000_000.0
    _wire(monkeypatch, tmp_path, posts, now)
    assert rn.main() == 0
    assert posts == [], "no backlog and no stuck item should post nothing"
    seen = json.loads((tmp_path / "pending_seen.json").read_text())
    assert seen == {"renovate/promtail": now}


def test_an_aged_clock_reaches_discord_and_names_the_item(monkeypatch, tmp_path):
    posts = []
    now = 1_000_000.0
    (tmp_path / "pending_seen.json").write_text(
        json.dumps({"renovate/promtail": now - 111 * DAY})
    )
    _wire(monkeypatch, tmp_path, posts, now)
    assert rn.main() == 0
    assert len(posts) == 1, "a 111-day-pending item must post"
    assert "grafana/promtail" in posts[0]
    assert "111 days" in posts[0]


def test_an_item_inside_its_allowance_stays_silent(monkeypatch, tmp_path):
    posts = []
    now = 1_000_000.0
    (tmp_path / "pending_seen.json").write_text(
        json.dumps(
            {"renovate/promtail": now - 10 * DAY}
        )  # under the 7+7 version allowance
    )
    _wire(monkeypatch, tmp_path, posts, now)
    assert rn.main() == 0
    assert posts == []


def test_the_clock_survives_across_runs(monkeypatch, tmp_path):
    """The dwell must accumulate, not restart: this is the failure the check exists to catch."""
    posts = []
    start = 1_000_000.0
    _wire(monkeypatch, tmp_path, posts, start)
    rn.main()
    _wire(monkeypatch, tmp_path, posts, start + 5 * DAY)
    rn.main()
    seen = json.loads((tmp_path / "pending_seen.json").read_text())
    assert seen == {"renovate/promtail": start}
    assert posts == []
    # ...and once the accumulated dwell passes the allowance, it fires.
    _wire(monkeypatch, tmp_path, posts, start + 20 * DAY)
    rn.main()
    assert len(posts) == 1


def test_a_dry_run_does_not_touch_the_clock(monkeypatch, tmp_path):
    posts = []
    _wire(monkeypatch, tmp_path, posts, 1_000_000.0)
    monkeypatch.setattr(rn.sys, "argv", ["renovate_notify.py", "--dry-run"])
    assert rn.main() == 0
    assert not (tmp_path / "pending_seen.json").exists()


def test_read_pending_seen_degrades_on_a_corrupt_file(tmp_path):
    """A corrupt clock must delay this check, never take the daily digest down with it."""
    path = tmp_path / "pending_seen.json"
    path.write_text("{not json")
    assert rn.read_pending_seen(str(path)) == {}
    path.write_text('["a", "list"]')
    assert rn.read_pending_seen(str(path)) == {}
    assert rn.read_pending_seen(str(tmp_path / "absent.json")) == {}


def test_read_pending_seen_round_trips_what_write_wrote(tmp_path):
    path = str(tmp_path / "pending_seen.json")
    rn.write_pending_seen(path, {"renovate/x": 12.5})
    assert rn.read_pending_seen(path) == {"renovate/x": 12.5}
