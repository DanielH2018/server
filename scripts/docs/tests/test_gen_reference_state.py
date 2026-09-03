"""Tests for scripts/docs/reference/state.py.

Fixture-driven: synthetic state directories under tmp_path, never the live host paths this
generator reads in production.

Run: uv run pytest scripts/docs/tests/test_gen_reference_state.py
"""

from __future__ import annotations

import datetime as dt
import textwrap

from docs.reference import crons
from docs.reference import state as g

NOW = dt.datetime(2026, 9, 3, 12, 0, 0, tzinfo=dt.timezone.utc)


# --- status_for(): the four statuses, each with an input that must land there -------------


def test_a_fresh_marker_is_ok(tmp_path):
    (tmp_path / "last_run").write_text(str((NOW - dt.timedelta(minutes=5)).timestamp()))
    run = g._epoch_marker(tmp_path, "last_run", "ticked")
    assert g.status_for(run, cadence=10.0, now=NOW) == "ok"


def test_a_stale_marker_is_late():
    """More than 2x cadence old -- the one rule every loop is judged by."""
    run = g.LoopRun(NOW - dt.timedelta(minutes=25), "ticked")
    assert g.status_for(run, cadence=10.0, now=NOW) == "late"


def test_a_missing_marker_file_is_never(tmp_path):
    """The state DIRECTORY exists but the file does not: the loop has never completed a run."""
    run = g._epoch_marker(tmp_path, "last_run", "ticked")
    assert g.status_for(run, cadence=10.0, now=NOW) == "never"
    assert run.last_run is None
    assert not run.unreadable


def test_a_missing_state_directory_is_unreadable(tmp_path):
    """The directory itself does not exist: this checkout cannot reach the loop's evidence."""
    run = g._epoch_marker(tmp_path / "does-not-exist", "last_run", "ticked")
    assert g.status_for(run, cadence=10.0, now=NOW) == "unreadable"
    assert run.unreadable


def test_unparseable_content_is_unreadable(tmp_path):
    (tmp_path / "last_run").write_text("not-a-number")
    run = g._epoch_marker(tmp_path, "last_run", "ticked")
    assert g.status_for(run, cadence=10.0, now=NOW) == "unreadable"


def test_unknown_cadence_reads_ok_when_it_has_ever_run():
    """A loop this generator cannot derive a cadence for is never called 'late'."""
    run = g.LoopRun(NOW - dt.timedelta(days=400), "ticked")
    assert g.status_for(run, cadence=None, now=NOW) == "ok"


# --- gitops_deploy_run(): the hold_sha/hold_plane warning path -----------------------------


def test_gitops_deploy_with_no_hold_is_a_plain_tick(tmp_path):
    (tmp_path / "last_run").write_text(str(NOW.timestamp()))
    run = g.gitops_deploy_run(tmp_path)
    assert run.outcome == "ticked, no hold"
    assert "HOLD" not in run.outcome


def test_gitops_deploy_with_a_hold_surfaces_the_sha_and_plane(tmp_path):
    (tmp_path / "last_run").write_text(str(NOW.timestamp()))
    (tmp_path / "hold_sha").write_text("deadbeefcafe1234\n")
    (tmp_path / "hold_plane").write_text("k8s/rollout-drain\n")
    run = g.gitops_deploy_run(tmp_path)
    assert "HOLD" in run.outcome
    assert "deadbeef" in run.outcome
    assert "k8s/rollout-drain" in run.outcome


# --- renovate_notify_run(): notified vs. checked-and-quiet ----------------------------------


def test_renovate_notify_with_no_fingerprint_reads_checked(tmp_path):
    (tmp_path / "last_run").write_text(str(NOW.timestamp()))
    assert g.renovate_notify_run(tmp_path).outcome == "checked, nothing new to notify"


def test_renovate_notify_with_a_fingerprint_reads_notified(tmp_path):
    (tmp_path / "last_run").write_text(str(NOW.timestamp()))
    (tmp_path / "last_notified").write_text("abc123")
    assert g.renovate_notify_run(tmp_path).outcome == "notified"


# --- docs_refresh_run(): build-info.json ----------------------------------------------------


def test_docs_refresh_reads_built_at_and_generators(tmp_path):
    info = tmp_path / "build-info.json"
    info.write_text('{"built_at": "2026-09-03 06:17 UTC", "generators": "ok"}')
    run = g.docs_refresh_run(info)
    assert run.last_run == dt.datetime(2026, 9, 3, 6, 17, tzinfo=dt.timezone.utc)
    assert run.outcome == "generators: ok"


def test_docs_refresh_missing_file_is_unreadable(tmp_path):
    run = g.docs_refresh_run(tmp_path / "build-info.json")
    assert run.unreadable


def test_docs_refresh_malformed_json_is_unreadable(tmp_path):
    info = tmp_path / "build-info.json"
    info.write_text("not json")
    run = g.docs_refresh_run(info)
    assert run.unreadable


# --- etcd_restore_drill_run(): the key=value stamp format ------------------------------------


def test_etcd_drill_parses_the_key_value_stamp(tmp_path):
    (tmp_path / "last-success-list-only").write_text(
        textwrap.dedent(
            """\
            mode=list-only
            snapshot=offbox-daniel-box-1788144302.zip
            utc=2026-08-31T10:20:03Z
            epoch=1788171603
            """
        )
    )
    run = g.etcd_restore_drill_run(tmp_path)
    assert run.last_run == dt.datetime.fromtimestamp(1788171603, dt.timezone.utc)
    assert "offbox-daniel-box-1788144302.zip" in run.outcome


# --- cadence_minutes(): reused from crons.py, not duplicated --------------------------------


def test_cadence_twice_daily_from_a_comma_hour_list():
    assert crons.cadence_minutes("17 6,18 * * *") == 720.0


def test_state_cron_job_cadence_calls_the_shared_parser(tmp_path):
    """`_cron_job_cadence` must go through crons.cadence_minutes, not a private copy."""
    role = tmp_path / "docsrefresh" / "tasks"
    role.mkdir(parents=True)
    (role / "main.yml").write_text(
        textwrap.dedent(
            """\
            ---
            - name: Schedule it
              ansible.builtin.cron:
                name: "Refresh generated docs"
                minute: "17"
                hour: "6,18"
                job: "/bin/true"
            """
        )
    )
    assert g._cron_job_cadence("Refresh generated docs", tmp_path) == 720.0


def test_cadence_hourly_from_minute_only():
    assert crons.cadence_minutes("5 * * * *") == 60.0


def test_cadence_daily_from_fixed_hour_and_minute():
    assert crons.cadence_minutes("9 4 * * *") == 1440.0


def test_cadence_weekly_from_a_restricted_weekday():
    assert crons.cadence_minutes("0 9 * * 0") == 10080.0


def test_cadence_step_minutes():
    assert crons.cadence_minutes("*/15 * * * *") == 15.0


def test_cadence_unknown_for_all_wildcards():
    assert crons.cadence_minutes("* * * * *") is None


# --- non-vacuity: the real cron jobs state.py matches by name must still exist -------------


def test_live_crons_still_carry_the_jobs_state_matches_by_name():
    """A rename of any of these silently drops that loop's cadence to 'unknown'.

    Guards against the failure class this repo has paid for repeatedly: a name-matching
    lookup returning an empty/None result reads as a legitimate 'unknown cadence' rather
    than as the job having moved or been renamed.
    """
    names = {row["name"] for row in crons.build_rows()}
    required = {
        "Refresh generated docs",
        "Weekly secret rotation (auto tier)",
        "Longhorn restore drill",
        "etcd restore drill",
    }
    assert required <= names


def test_live_k3s_defaults_still_carry_the_drill_cron_vars():
    assert g._k3s_default_var("k3s_longhorn_restore_drill_cron") is not None
    assert g._k3s_default_var("k3s_etcd_restore_drill_cron") is not None


def test_build_rows_on_the_live_tree_yields_at_least_six_named_loops():
    rows = g.build_rows()
    names = {r["name"] for r in rows}
    assert len(rows) >= 6
    assert names == {
        "gitops-deploy",
        "renovate-agent",
        "renovate-notify",
        "docs-refresh",
        "secret-rotate",
        "longhorn-restore-drill",
        "etcd-restore-drill",
    }


# --- rendering --------------------------------------------------------------------------


def test_markdown_opens_with_the_provenance_banner():
    rows = g.build_rows(now=NOW)
    out = g.render_markdown(rows)
    assert out.startswith("---\n")
    assert "generated_from: scripts/docs/reference/state.py" in out


def test_markdown_summary_line_counts_ok_loops():
    rows = [
        {
            "name": "a",
            "last_run": "x",
            "age": "1m",
            "cadence": "10m",
            "status": "ok",
            "outcome": "fine",
        },
        {
            "name": "b",
            "last_run": "never",
            "age": "—",
            "cadence": "10m",
            "status": "never",
            "outcome": "no run recorded yet",
        },
    ]
    out = g.render_markdown(rows)
    assert "1 of 2 loops within cadence." in out


def test_markdown_ends_with_exactly_one_newline():
    out = g.render_markdown(g.build_rows(now=NOW))
    assert out.endswith("\n")
    assert not out.endswith("\n\n")


def test_a_pipe_in_the_outcome_does_not_split_the_row():
    rows = [
        {
            "name": "a",
            "last_run": "x",
            "age": "1m",
            "cadence": "10m",
            "status": "ok",
            "outcome": "a | b",
        }
    ]
    out = g.render_markdown(rows)
    line = [line for line in out.splitlines() if line.startswith("| a |")][0]
    # 7 column delimiters plus the one escaped pipe inside the outcome cell.
    assert line.count("|") == 8
    assert "a \\| b" in line
