"""Tests for scripts/gen_reference_crons.py.

Fixture-driven: synthetic roles under tmp_path, never the real tree, which changes.

Run: uv run pytest scripts/test_gen_reference_crons.py
"""

from __future__ import annotations

import textwrap

import gen_reference_crons as g


def _role(tmp_path, role: str, body: str):
    path = tmp_path / role / "tasks" / "main.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body))
    return path


def _basic(tmp_path):
    _role(
        tmp_path,
        "alpha",
        """\
        ---
        - name: Schedule a read-only check
          when: inventory_hostname == 'daniel-box'
          ansible.builtin.cron:
            name: "Check something"
            minute: "5"
            hour: "3"
            user: ubuntu
            job: "/usr/local/bin/probe --check"
        """,
    )
    _role(
        tmp_path,
        "beta",
        """\
        ---
        - name: Schedule a committing job
          ansible.builtin.cron:
            name: "Rotate secrets"
            special_time: weekly
            user: root
            job: "cd /repo && git commit -am x && git push"
        """,
    )
    return tmp_path


def test_finds_cron_tasks_across_roles(tmp_path):
    rows = g.build_rows(_basic(tmp_path))
    assert {r["name"] for r in rows} == {"Check something", "Rotate secrets"}


def test_records_the_defining_file(tmp_path):
    """Without it the page says a job exists but not where to change it."""
    rows = {r["name"]: r for r in g.build_rows(_basic(tmp_path))}
    assert rows["Check something"]["source"].endswith("alpha/tasks/main.yml")


def test_five_field_schedule_and_special_time(tmp_path):
    rows = {r["name"]: r for r in g.build_rows(_basic(tmp_path))}
    assert rows["Check something"]["schedule"] == "5 3 * * *"
    assert rows["Rotate secrets"]["schedule"] == "@weekly"


def test_host_comes_from_the_when_clause(tmp_path):
    rows = {r["name"]: r for r in g.build_rows(_basic(tmp_path))}
    assert rows["Check something"]["host"] == "daniel-box"
    assert rows["Rotate secrets"]["host"] == "every host in the play"


def test_state_changing_jobs_are_flagged(tmp_path):
    rows = {r["name"]: r for r in g.build_rows(_basic(tmp_path))}
    assert rows["Rotate secrets"]["changes_state"].startswith("yes")
    assert "git commit" in rows["Rotate secrets"]["changes_state"]


def test_a_wrapper_script_is_not_guessed_at(tmp_path):
    """A job whose command is a script says so instead of claiming read-only.

    Clearing a wrapper as harmless is the failure that costs something; saying
    'read the script' costs a reader one moment.
    """
    _role(
        tmp_path,
        "gamma",
        """\
        ---
        - name: Schedule a wrapper
          ansible.builtin.cron:
            name: "Wrapped"
            minute: "0"
            job: "/usr/local/bin/some-wrapper.sh"
        """,
    )
    rows = {r["name"]: r for r in g.build_rows(tmp_path)}
    assert rows["Wrapped"]["changes_state"] == "read the script"


def test_absent_crons_are_skipped(tmp_path):
    """`state: absent` removes a job; listing it would describe the opposite of reality."""
    _role(
        tmp_path,
        "delta",
        """\
        ---
        - name: Remove an old cron
          ansible.builtin.cron:
            name: "Retired job"
            state: absent
        """,
    )
    assert "Retired job" not in {r["name"] for r in g.build_rows(tmp_path)}


def test_archive_roles_are_skipped(tmp_path):
    """roles/containers/archive/ is retired code, not installed crons."""
    _role(
        tmp_path,
        "containers/archive/kopia",
        """\
        ---
        - name: Schedule a retired backup
          ansible.builtin.cron:
            name: "Old backup"
            minute: "0"
            job: "/usr/local/bin/backup.sh"
        """,
    )
    assert "Old backup" not in {r["name"] for r in g.build_rows(tmp_path)}


def test_unresolved_jinja_is_printed_not_guessed(tmp_path):
    """These pages never run Ansible, so a template is the honest rendering."""
    _role(
        tmp_path,
        "epsilon",
        """\
        ---
        - name: Templated schedule
          ansible.builtin.cron:
            name: "Templated"
            minute: "{{ some_minute }}"
            job: "/bin/true"
        """,
    )
    rows = {r["name"]: r for r in g.build_rows(tmp_path)}
    assert "{{ some_minute }}" in rows["Templated"]["schedule"]


def test_markdown_opens_with_the_provenance_banner(tmp_path):
    out = g.render_markdown(g.build_rows(_basic(tmp_path)))
    assert out.startswith("---\n")
    assert "generated_from: scripts/gen_reference_crons.py" in out


def test_markdown_says_the_state_column_is_a_heuristic(tmp_path):
    """An inferred column presented as authoritative is worse than none."""
    out = g.render_markdown(g.build_rows(_basic(tmp_path)))
    assert "heuristic" in out.lower()


def test_markdown_ends_with_exactly_one_newline(tmp_path):
    out = g.render_markdown(g.build_rows(_basic(tmp_path)))
    assert out.endswith("\n")
    assert not out.endswith("\n\n")
