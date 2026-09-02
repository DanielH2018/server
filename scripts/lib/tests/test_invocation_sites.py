"""Each reader below is a (accept, reject) pair, per the repo-root CLAUDE.md rule that a new
check ships with proof it can go RED: one fixture it must find, one it must NOT find, so a
reader that silently stopped matching (or started over-matching) fails its own test instead of
reading green forever.
"""

from __future__ import annotations

import textwrap

from invocation_sites import (
    claude_hook_files,
    claude_settings_entries,
    cron_jobs,
    prek_hook_entries,
    sh_j2_templates,
    systemd_exec_lines,
    workflow_run_steps,
)


def _write(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body))


# --- prek.toml ---------------------------------------------------------------------------


def test_prek_hook_entries_finds_a_real_entry(tmp_path):
    _write(
        tmp_path / "prek.toml",
        """
        [[repos]]
        [[repos.hooks]]
        id = "validate-thing"
        entry = "uv run python scripts/validate/validate_thing.py"
        """,
    )
    found = dict(prek_hook_entries(tmp_path))
    assert found["prek.toml hook 'validate-thing' entry="] == (
        "uv run python scripts/validate/validate_thing.py"
    )


def test_prek_hook_entries_skips_a_hook_with_no_entry(tmp_path):
    _write(
        tmp_path / "prek.toml",
        """
        [[repos]]
        [[repos.hooks]]
        id = "trailing-whitespace"
        """,
    )
    assert prek_hook_entries(tmp_path) == []


# --- GitHub Actions workflows --------------------------------------------------------------


def test_workflow_run_steps_finds_a_run_field(tmp_path):
    _write(
        tmp_path / ".github" / "workflows" / "ci.yml",
        """
        jobs:
          test:
            steps:
              - run: uv run python scripts/dev/thing.py
        """,
    )
    out = workflow_run_steps(tmp_path)
    assert out == [
        (
            ".github/workflows/ci.yml job 'test' step 0",
            "uv run python scripts/dev/thing.py",
        )
    ]


def test_workflow_run_steps_skips_a_step_with_no_run_field(tmp_path):
    _write(
        tmp_path / ".github" / "workflows" / "ci.yml",
        """
        jobs:
          test:
            steps:
              - uses: actions/checkout@v4
        """,
    )
    assert workflow_run_steps(tmp_path) == []


# --- ansible.builtin.cron -------------------------------------------------------------------


def test_cron_jobs_is_tree_wide_not_just_initial_setup(tmp_path):
    """traefik, claude-otel and eight other roles carry their own cron tasks -- a reader
    scoped to `initial_setup/tasks/crons.yml` alone misses all of them."""
    _write(
        tmp_path / "ansible" / "roles" / "traefik" / "tasks" / "main.yml",
        """
        - name: Rotate something
          ansible.builtin.cron:
            name: Rotate something
            job: "uv run python scripts/dev/rotate.py"
        """,
    )
    jobs = cron_jobs(tmp_path)
    assert [j.job for j in jobs] == ["uv run python scripts/dev/rotate.py"]
    assert jobs[0].name == "Rotate something"


def test_cron_jobs_skips_an_absent_cron(tmp_path):
    _write(
        tmp_path / "ansible" / "roles" / "r" / "tasks" / "main.yml",
        """
        - name: Retired job
          ansible.builtin.cron:
            name: Retired job
            job: "uv run python scripts/dev/retired.py"
            state: absent
        """,
    )
    assert cron_jobs(tmp_path) == []


def test_cron_jobs_skips_an_archived_role(tmp_path):
    _write(
        tmp_path / "ansible" / "roles" / "archive" / "old" / "tasks" / "main.yml",
        """
        - name: Old job
          ansible.builtin.cron:
            name: Old job
            job: "uv run python scripts/dev/old.py"
        """,
    )
    assert cron_jobs(tmp_path) == []


# --- shell-wrapper templates ---------------------------------------------------------------


def test_sh_j2_templates_finds_a_template_anywhere_in_the_tree(tmp_path):
    _write(
        tmp_path / "ansible" / "roles" / "r" / "templates" / "wrap.sh.j2",
        "#!/bin/sh\nuv run python scripts/dev/wrapped.py\n",
    )
    found = sh_j2_templates(tmp_path)
    assert [p.name for p in found] == ["wrap.sh.j2"]


def test_sh_j2_templates_skips_an_archived_template(tmp_path):
    _write(
        tmp_path / "ansible" / "roles" / "archive" / "r" / "templates" / "wrap.sh.j2",
        "#!/bin/sh\nuv run python scripts/dev/wrapped.py\n",
    )
    assert sh_j2_templates(tmp_path) == []


# --- .claude/hooks wrappers ------------------------------------------------------------------


def test_claude_hook_files_finds_a_wrapper(tmp_path):
    _write(tmp_path / ".claude" / "hooks" / "block-thing.py", "print('hi')\n")
    found = claude_hook_files(tmp_path)
    assert [p.name for p in found] == ["block-thing.py"]


def test_claude_hook_files_skips_a_test_file(tmp_path):
    _write(tmp_path / ".claude" / "hooks" / "test_block_thing.py", "print('hi')\n")
    assert claude_hook_files(tmp_path) == []


# --- systemd units -----------------------------------------------------------------------


def test_systemd_exec_lines_finds_an_execstart_line(tmp_path):
    _write(
        tmp_path / "ansible" / "roles" / "r" / "templates" / "thing.service.j2",
        """
        [Unit]
        Description=Thing
        [Service]
        ExecStart=/opt/thing/run.py
        """,
    )
    out = systemd_exec_lines(tmp_path)
    assert len(out) == 1
    assert out[0][1].strip() == "ExecStart=/opt/thing/run.py"


def test_systemd_exec_lines_skips_a_non_execstart_line(tmp_path):
    _write(
        tmp_path / "ansible" / "roles" / "r" / "templates" / "thing.service.j2",
        """
        [Unit]
        Description=scripts/dev/thing.py is not an ExecStart line
        """,
    )
    assert systemd_exec_lines(tmp_path) == []


# --- .claude/settings*.json permissions -----------------------------------------------------


def test_claude_settings_entries_finds_an_allow_entry(tmp_path):
    _write(
        tmp_path / ".claude" / "settings.json",
        """
        {"permissions": {"allow": ["Bash(uv run python scripts/dev/thing.py:*)"]}}
        """,
    )
    out = claude_settings_entries(tmp_path)
    assert out == [
        (
            ".claude/settings.json permissions.allow",
            "Bash(uv run python scripts/dev/thing.py:*)",
        )
    ]


def test_claude_settings_entries_is_empty_with_no_permissions_block(tmp_path):
    _write(tmp_path / ".claude" / "settings.json", "{}")
    assert claude_settings_entries(tmp_path) == []
