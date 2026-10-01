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
import pending_logic as pl
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


# The digest-clock key `main()` writes beside the branch key for the fixture's one item: the
# dwell is keyed on the update, so the state file carries both clocks.
_PROMTAIL_DIGEST_KEY = pl.content_key(
    "renovate/promtail", "Update grafana/promtail Docker tag to v3.6.11"
)

# A dashboard whose one pending item is a mutable-tag digest bump on a branch Renovate reuses.
CHURN_DASHBOARD = {
    "title": "Dependency Dashboard",
    "user": {"login": "renovate[bot]"},
    "updated_at": "2999-01-01T00:00:00Z",
    "body": (
        "## Pending Status Checks\n\n"
        " - [ ] <!-- unpend-branch=renovate/k8s-image-nginx -->"
        "Update nginx:alpine Docker digest to bbbbbbb\n"
    ),
}


def _wire(monkeypatch, tmp_path, posts, now, dashboard=DASHBOARD):
    """Point main() at a fake GitHub + Discord and a state dir under tmp_path.

    `dashboard` selects which captured body the fake GitHub returns. Patching it here rather
    than re-patching `get` in a wrapper keeps the module's monkeypatch count where
    `test_no_test_module_patches_more_modules_than_its_allowlist_entry` holds it.
    """
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
    monkeypatch.setattr(rn, "get", lambda url: [dashboard] if "/issues" in url else [])
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
    assert seen == {"renovate/promtail": now, _PROMTAIL_DIGEST_KEY: now}


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
    assert seen == {"renovate/promtail": start, _PROMTAIL_DIGEST_KEY: start}
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


def test_clamp_leaves_a_short_message_alone():
    assert rn.clamp_for_discord("hello") == "hello"


def test_clamp_trims_a_message_past_discords_cap():
    """The renderers each bound their own part; four parts joined can still overflow."""
    clamped = rn.clamp_for_discord("x" * 5000)
    assert len(clamped) <= rn.DISCORD_LIMIT
    assert clamped.endswith("…(truncated)")


# --- Dwell-state loss ---------------------------------------------------------
# `pending_state_lost` is the only thing telling a wiped clock apart from the intended first-run
# bootstrap, and both halves are load-bearing: firing on a legitimately empty map pages every
# quiet day, and not firing on a lost one leaves the arm inert for up to 14 days in silence.


def test_a_lost_clock_is_flagged_on_a_host_that_has_run_before(tmp_path):
    (tmp_path / "last_run").write_text("1000000.0")
    seen = tmp_path / "pending_seen.json"
    run = tmp_path / "last_run"
    assert rn.pending_state_lost(str(seen), str(run)), "a missing file is a loss"
    seen.write_text("{not json")
    assert rn.pending_state_lost(str(seen), str(run)), "an unparseable file is a loss"
    seen.write_text('["a", "list"]')
    assert rn.pending_state_lost(str(seen), str(run)), "a non-map file is a loss"


def test_an_empty_clock_is_clean(tmp_path):
    """`{}` is what a run with nothing pending writes — a quiet day, never a loss."""
    (tmp_path / "last_run").write_text("1000000.0")
    (tmp_path / "pending_seen.json").write_text("{}")
    assert not rn.pending_state_lost(
        str(tmp_path / "pending_seen.json"), str(tmp_path / "last_run")
    )
    (tmp_path / "pending_seen.json").write_text('{"renovate/x": 1.0}')
    assert not rn.pending_state_lost(
        str(tmp_path / "pending_seen.json"), str(tmp_path / "last_run")
    )


def test_a_genuine_first_run_is_clean(tmp_path):
    """No `last_run` means the notifier has never completed here: seeding is not a loss."""
    assert not rn.pending_state_lost(
        str(tmp_path / "pending_seen.json"), str(tmp_path / "last_run")
    )


def test_a_wiped_clock_reaches_discord_and_names_the_date(monkeypatch, tmp_path):
    """The run reports the reset rather than completing healthy.

    The item below is inside its allowance, so `stuck_pending` is empty — exactly the state
    the reset creates, and the one that used to post nothing at all.
    """
    posts = []
    now = 1_788_990_155.26  # 2026-09-09 21:42 UTC
    (tmp_path / "last_run").write_text(str(now - DAY))
    _wire(monkeypatch, tmp_path, posts, now)
    assert rn.main() == 0
    assert len(posts) == 1, "a lost dwell clock must post"
    assert "2026-09-23" in posts[0], "the post must name when the check works again"


def test_a_first_run_does_not_report_a_reset(monkeypatch, tmp_path):
    posts = []
    _wire(monkeypatch, tmp_path, posts, 1_788_990_155.26)
    assert rn.main() == 0
    assert posts == [], "seeding an empty state file is not a loss"


def test_a_churning_branch_reaches_discord_when_no_digest_ever_ages_out(
    monkeypatch, tmp_path
):
    """The arm the digest clock makes necessary, end to end.

    The branch has been in the section 30 days — past (1-day soak + 7-day grace) * 3 — while
    the digest on it arrived two days ago, so the dwell arm cannot fire and this one must.
    """
    posts = []
    now = 1_000_000.0
    branch = "renovate/k8s-image-nginx"
    (tmp_path / "pending_seen.json").write_text(
        json.dumps(
            {
                branch: now - 30 * DAY,
                pl.content_key(
                    branch, "Update nginx:alpine Docker digest to bbbbbbb"
                ): (now - 2 * DAY),
            }
        )
    )
    _wire(monkeypatch, tmp_path, posts, now, dashboard=CHURN_DASHBOARD)
    assert rn.main() == 0
    assert len(posts) == 1, "a 30-day churning branch must post"
    assert "branch pending 30 days" in posts[0]
    assert "only 2" in posts[0]
    assert branch in posts[0]


def test_a_re_pushed_branch_inside_the_churn_allowance_stays_silent(
    monkeypatch, tmp_path
):
    """The same inputs, 20 days in: neither arm may fire.

    A dwell keyed on the branch clock would post here: 20 days is past the 1+7
    allowance the digest on the branch has never come close to.
    """
    posts = []
    now = 1_000_000.0
    branch = "renovate/k8s-image-nginx"
    (tmp_path / "pending_seen.json").write_text(
        json.dumps(
            {
                branch: now - 20 * DAY,
                pl.content_key(
                    branch, "Update nginx:alpine Docker digest to bbbbbbb"
                ): (now - 2 * DAY),
            }
        )
    )
    _wire(monkeypatch, tmp_path, posts, now, dashboard=CHURN_DASHBOARD)
    assert rn.main() == 0
    assert posts == []
