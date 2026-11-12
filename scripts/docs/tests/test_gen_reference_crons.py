"""Tests for scripts/docs/reference/crons.py.

Run: uv run pytest scripts/docs/tests/test_gen_reference_crons.py
"""

import re

from docs.reference import crons as g


def _row(**overrides):
    row = {
        "name": "homelab-example.timer",
        "schedule": "OnCalendar=daily",
        "host": "daniel-box",
        "user": "root",
        "changes_state": "read the script",
        "source": "hosts/components/example/units/homelab-example.timer",
    }
    return row | overrides


def test_markdown_opens_with_the_provenance_banner():
    out = g.render_markdown([_row()])
    assert out.startswith("---\n")
    assert "generated_from: scripts/docs/reference/crons.py" in out


def test_a_pipe_in_a_cell_stays_in_its_cell():
    out = g.render_markdown([_row(host="a | b")])
    row = next(
        line for line in out.splitlines() if line.startswith("| homelab-example")
    )
    cells = re.split(r"(?<!\\)\|", row)[1:-1]
    assert len(cells) == 6


def test_markdown_ends_with_exactly_one_newline():
    out = g.render_markdown([_row()])
    assert out.endswith("\n")
    assert not out.endswith("\n\n")


def test_a_timer_whose_command_names_only_a_wrapper_needs_the_script_read():
    service = "[Service]\nExecStart=/usr/lib/homelab/docs-refresh\n"
    assert g._timer_changes_state(service) == "read the script"


def test_a_timer_whose_command_names_a_mutating_word_is_flagged():
    service = "[Service]\nExecStart=/usr/lib/homelab/secret-rotate\n"
    assert g._timer_changes_state(service) == "yes (rotate)"


def test_the_live_page_lists_a_packaged_timer(tmp_path):
    """A named member: an empty read of the packages fails here."""
    out = tmp_path / "crons.md"
    assert g.main(["--out", str(out)]) == 0
    page = out.read_text()
    assert (
        "| homelab-docs-refresh.timer | `OnCalendar=*-*-* 06,18:17:00` | daniel-box |"
        in page
    )


def test_hosts_that_differ_on_a_timers_schedule_get_a_row_each():
    rows = [r for r in g.build_timer_rows() if r["name"] == "homelab-updater.timer"]
    assert {r["host"] for r in rows} == {"daniel-box, daniel-server", "daniel-pi"}
