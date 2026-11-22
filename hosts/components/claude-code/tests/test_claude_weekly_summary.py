"""claude_weekly_summary.run over run records shaped as their writers write them.

The Renovate ticks come from `agent_logic.run_record` itself. The triage record is built inline in
`claude_triage_run.main`, so `triage_record` copies its fields.
"""

import datetime as dt
import json
import re

import claude_weekly_summary as summary
from _setup_render import COMPONENTS, render_component_text
from agent_logic import Delta, Outcome, run_record

# Monday 2026-11-16 09:00 UTC: the run covers 2026-11-09 00:00 to 2026-11-15 23:59.
NOW = dt.datetime(2026, 11, 16, 9, 0, tzinfo=dt.UTC).timestamp()


def _epoch(text: str) -> int:
    return int(dt.datetime.fromisoformat(text).replace(tzinfo=dt.UTC).timestamp())


def triage_record(
    episode_id, episode, state, started, diagnosis=None, cost=None, denials=0
):
    return {
        "episode_id": episode_id,
        "episode": episode,
        "state": state,
        "diagnosis": diagnosis,
        "error": None if diagnosis else "the session returned no diagnosis",
        "session_id": None if state == "capped" else "s-1",
        "subtype": "success",
        "cost_usd": cost,
        "num_turns": None if state == "capped" else 12,
        "denials": denials,
        "transcript_bytes": None,
        "transcript_cut": False,
        "duration_s": 300,
        "started": started,
        "finished": started,
        "report": f"https://artifacts.example/triage-{episode_id}.html",
    }


class World:
    def __init__(self, tmp_path):
        self.runs = tmp_path / "triage" / "runs"
        self.log = tmp_path / "renovate" / "runs.jsonl"
        self.config = {
            "sources": [
                {"name": "triage", "kind": "triage", "path": str(self.runs)},
                {"name": "renovate-agent", "kind": "renovate", "path": str(self.log)},
            ],
            "state_dir": str(tmp_path / "state"),
            "deadline_s": 608400,
        }
        self.posts = []
        self.deliver = True

    def triage(self, record):
        self.runs.mkdir(parents=True, exist_ok=True)
        (self.runs / f"{record['episode_id']}.json").write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n"
        )

    def tick(self, line):
        self.log.parent.mkdir(parents=True, exist_ok=True)
        with self.log.open("a") as f:
            f.write(line + "\n")

    def run(self, now=NOW):
        def post(content):
            self.posts.append(content)
            return self.deliver

        return summary.run(self.config, now, "daniel-box", post)


def test_a_drill_weeks_records_render_the_summary(tmp_path):
    # fact: hosts/components/claude-code/CLAUDE.md#Autonomous-role contract (`claude-weekly-summary`, one post a week about the unattended runs)
    world = World(tmp_path)
    world.triage(
        triage_record(
            "drill-1",
            {
                "monitor": "ExtraProbeFailed",
                "service": "sonarr",
                "severity": "critical",
            },
            "diagnosed",
            "2026-11-10 14:02 UTC",
            {
                "summary": "sonarr's probe fails on a 502",
                "root_cause": "x",
                "confidence": "high",
            },
            cost=1.25,
            denials=2,
        )
    )
    world.triage(
        triage_record(
            "drill-2",
            {"storm": True, "down": ["a", "b", "c", "d"]},
            "capped",
            "2026-11-11 08:30 UTC",
        )
    )
    world.tick(
        run_record(_epoch("2026-11-10T06:00"), "skipped", "no open Renovate PRs")
    )
    world.tick(
        run_record(
            _epoch("2026-11-12T06:00"),
            "ran",
            moved=Delta(resolved=(101, 102), remaining=(103,), opened=()),
            outcome=Outcome(
                ok=True, summary="", cost_usd=3.5, turns=40, denials=("Bash",)
            ),
        )
    )

    assert world.run() == (True, "2026-W46 posted: 4 runs")
    assert world.posts == [
        "claude-weekly-summary: 2026-W46 (2026-11-09 to 2026-11-15 UTC) on daniel-box\n"
        "triage: 2 runs, 1 session, $1.25, 2 denials\n"
        "renovate-agent: 2 runs, 1 session, $3.50, 1 denial\n"
        "triage:\n"
        "- ExtraProbeFailed:sonarr diagnosed; 2 denials: sonarr's probe fails on a 502"
        " <https://artifacts.example/triage-drill-1.html>\n"
        "- storm drill-2 capped: the session returned no diagnosis"
        " <https://artifacts.example/triage-drill-2.html>\n"
        "renovate-agent:\n"
        "- Thu 11-12 ran; 1 denial: merged #101, #102; left open #103"
    ]


def test_the_window_holds_sunday_2359_and_drops_the_next_monday_0000(tmp_path):
    # fact: hosts/components/claude-code/CLAUDE.md#Autonomous-role contract (`claude-weekly-summary`, one post a week about the unattended runs)
    world = World(tmp_path)
    for episode_id, started in (
        ("before", "2026-11-08 23:59 UTC"),
        ("first", "2026-11-09 00:00 UTC"),
        ("last", "2026-11-15 23:59 UTC"),
        ("after", "2026-11-16 00:00 UTC"),
    ):
        world.triage(triage_record(episode_id, {}, "inconclusive", started))
    for ts in ("2026-11-09T00:00", "2026-11-15T23:59", "2026-11-16T00:00"):
        world.tick(run_record(_epoch(ts), "skipped", "no open Renovate PRs"))

    world.run()

    assert "triage: 2 runs," in world.posts[0]
    assert "renovate-agent: 2 runs," in world.posts[0]
    assert "- first " in world.posts[0] and "- last " in world.posts[0]
    assert "before" not in world.posts[0] and "after" not in world.posts[0]


def test_a_catch_up_run_later_in_the_week_covers_the_same_week(tmp_path):
    # fact: hosts/components/claude-code/CLAUDE.md#Autonomous-role contract (`claude-weekly-summary`, one post a week about the unattended runs)
    world = World(tmp_path)
    tuesday = dt.datetime(2026, 11, 17, 3, 0, tzinfo=dt.UTC).timestamp()

    assert world.run(tuesday) == (True, "2026-W46 posted: 0 runs")


def test_a_quiet_week_still_posts_its_count_lines(tmp_path):
    # fact: hosts/components/claude-code/CLAUDE.md#Autonomous-role contract (`claude-weekly-summary`, one post a week about the unattended runs)
    world = World(tmp_path)

    assert world.run() == (True, "2026-W46 posted: 0 runs")
    assert world.posts == [
        "claude-weekly-summary: 2026-W46 (2026-11-09 to 2026-11-15 UTC) on daniel-box\n"
        "triage: 0 runs, 0 sessions, $0.00, 0 denials\n"
        "renovate-agent: 0 runs, 0 sessions, $0.00, 0 denials"
    ]


def test_a_mention_in_a_diagnosis_or_an_episode_is_defused(tmp_path):
    # fact: hosts/components/claude-code/CLAUDE.md#Autonomous-role contract (`claude-weekly-summary`, one post a week about the unattended runs)
    world = World(tmp_path)
    world.triage(
        triage_record(
            "e-1",
            {"monitor": "@everyone", "service": "<@&456>"},
            "diagnosed",
            "2026-11-10 10:00 UTC",
            {"summary": "ping @here <@123> in <#789>\nsecond line"},
        )
    )

    world.run()

    [post] = world.posts
    assert not re.search(r"@(everyone|here)|<@|<#", post)
    assert "second line" in post.splitlines()[-1]


def test_a_run_that_asked_for_a_restart_shows_the_brokers_verdict(tmp_path):
    # fact: hosts/components/claude-code/CLAUDE.md#Autonomous-role contract (`claude-weekly-summary`, one post a week about the unattended runs)
    world = World(tmp_path)
    for episode_id, restart in (
        ("e-1", {"name": "docs", "verdict": "restarted", "cause": "", "detail": "x"}),
        ("e-2", {"name": "<@123>", "verdict": "refused", "cause": "not-listed"}),
    ):
        record = triage_record(
            episode_id,
            {"monitor": "IngressProbeFailed", "service": "sonarr"},
            "diagnosed",
            "2026-11-10 10:00 UTC",
            {"summary": "s"},
        )
        world.triage({**record, "restart": restart})

    world.run()

    [post] = world.posts
    assert "IngressProbeFailed:sonarr diagnosed; restart docs restarted: s" in post
    assert "refused not-listed: s" in post
    assert "<@" not in post


def test_an_unparseable_record_is_counted_and_the_run_stays_up(tmp_path):
    # fact: hosts/components/claude-code/CLAUDE.md#Autonomous-role contract (`claude-weekly-summary`, one post a week about the unattended runs)
    world = World(tmp_path)
    world.runs.mkdir(parents=True)
    (world.runs / "broken.json").write_text("{not json")
    (world.runs / "broken.transcript.jsonl").write_text("{}\n")

    assert world.run()[0] is True
    assert (
        "triage: 0 runs, 0 sessions, $0.00, 0 denials (1 unreadable record)"
        in world.posts[0]
    )


def test_a_failed_post_reports_down_and_the_next_run_posts_again(tmp_path):
    # fact: hosts/components/claude-code/CLAUDE.md#Autonomous-role contract (`claude-weekly-summary`, one post a week about the unattended runs)
    world = World(tmp_path)
    world.deliver = False

    assert world.run() == (
        False,
        "2026-W46: the Discord post failed; the next run retries it",
    )

    world.deliver = True
    assert world.run() == (True, "2026-W46 posted: 0 runs")
    assert len(world.posts) == 2


def test_a_rerun_of_a_posted_week_posts_nothing(tmp_path):
    # fact: hosts/components/claude-code/CLAUDE.md#Autonomous-role contract (`claude-weekly-summary`, one post a week about the unattended runs)
    world = World(tmp_path)
    world.run()

    assert world.run() == (True, "2026-W46 already posted")
    assert len(world.posts) == 1


def test_the_config_reads_the_record_paths_the_controllers_pin():
    # fact: hosts/components/claude-code/CLAUDE.md#Autonomous-role contract (`claude-weekly-summary`, one post a week about the unattended runs)
    config = json.loads(
        render_component_text("claude-code", "claude-weekly-summary.json.j2")
    )
    paths = {source["kind"]: source["path"] for source in config["sources"]}

    def pin(component, template, name):
        text = (COMPONENTS / component / "templates" / template).read_text()
        [value] = re.findall(rf'set {name} = "([^"]+)"', text)
        return value

    assert paths == {
        "triage": pin("claude-code", "claude-triage-pins.j2", "state_dir") + "/runs",
        "renovate": pin("renovate-agent", "renovate-agent-pins.j2", "ctl_home")
        + "/runs.jsonl",
    }
    assert set(paths) == set(summary.READERS)
