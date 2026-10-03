"""Censuses of the role tree's tasks, templates and defaults, one `_row_table.Census` row each.

Each row was a test file of its own until #3430. A row's `reason` keeps what that file's
docstring said a reader needs before changing the rule. These rows read files under a role's
`templates/` directory for their literal text, so they sit apart from the other row files:
`test_guard_tests_read_renders_not_templates.py` exempts this module by name, with the reason.

Run: uv run pytest ansible/tests/repo/test_census_rows_roles.py
"""

import re

import pytest
from _helpers import walk_tasks
from _row_table import Census, Subject, check, proof_problems, tracked
from lib import yaml_fast


def _role_files(*directories: str) -> list[str]:
    """Every tracked file under `ansible/roles/<plane>/<role>/<directory>/`, at any depth."""
    return [
        rel
        for rel in tracked("ansible/roles/*")
        if len(parts := rel.split("/")) > 5 and parts[4] in directories
    ]


def _code(subject: Subject) -> list[tuple[int, str]]:
    """(line number, text before any `#`) for each line. A comment naming a file is not a ship."""
    return [
        (n, line.split("#", 1)[0])
        for n, line in enumerate(subject.text.splitlines(), 1)
    ]


# ── No role ships a test file or a markdown file ──────────────────────────────────────

# `src:`/`dest:` in a copy or template task, and the `lookup('file', ...)` a k8s ConfigMap
# inlines a script with.
SHIPPED_PY = re.compile(
    r"""(?:src|dest)\s*:\s*['"]?\S*?(?P<a>[\w.-]+\.py)"""
    r"""|lookup\(\s*['"]file['"]\s*,\s*['"]?\S*?(?P<b>[\w.-]+\.py)"""
)

# The same two shapes for a `.md`, plus a bare YAML list entry: `home-assistant`'s four
# `*_files` lists in `defaults/main.yml` name what its ConfigMap ships that way.
#
# WHAT THIS SCAN DOES NOT SEE: a ship that names no basename. `with_fileglob`/`fileglob` over a
# directory, a directory-shaped `src:` in a copy task, and `unarchive`/`synchronize` of a tree all
# put files on a host without a literal name to match. None of them ships a `.md` today — the two
# globs in the tree take `*.json` and `*.pub`, the three `unarchive` tasks take remote tarballs,
# and no task carries a directory `src:` — so the hole is latent rather than live. A `.md` added
# under one of those globs would pass this row and be skipped by the deployer, which is the one
# failure mode to check when a role starts globbing a directory that holds prose.
SHIPPED_MD = re.compile(
    r"""(?:src|dest)\s*:\s*['"]?\S*?(?P<a>[\w.-]+\.md)"""
    r"""|lookup\(\s*['"]file['"]\s*,\s*['"]?\S*?(?P<b>[\w.-]+\.md)"""
    r"""|^\s*-\s+['"]?(?P<c>[\w.-]+\.md)['"]?\s*$"""
)


def _shipped(pattern: re.Pattern[str], subject: Subject) -> list[tuple[int, str]]:
    """(line, basename) for every file `subject` names as one to put on a host."""
    return [
        (n, name)
        for n, code in _code(subject)
        for match in pattern.finditer(code)
        if (name := next(g for g in match.groups() if g))
    ]


def _is_test_file(name: str) -> bool:
    return name == "conftest.py" or name.startswith("test_")


# ── No role task writes a home-relative path ──────────────────────────────────────────

# The keys that name a path on the target host. `src:` is deliberately absent: for `copy:` and
# `template:` it names a file in the CONTROL node's role, where `~` never reaches sudo.
_PATH_KEYS = ("path", "dest")


def _home_relative_paths(subject: Subject) -> list[str]:
    """Every `~`-relative target a task in this tasks file writes, whichever module it uses."""
    hits = []
    for task in walk_tasks(yaml_fast.safe_load(subject.text) or []):
        for key, value in task.items():
            if not isinstance(value, dict):
                continue
            hits += [
                f"{task.get('name')}: {key}.{path_key}={target}"
                for path_key in _PATH_KEYS
                if isinstance(target := value.get(path_key), str)
                and target.startswith("~")
            ]
    return hits


ROWS = (
    Census(
        name="no-role-ships-a-test-file",
        reason=(
            "`deploy_logic._is_test_only_path` skips test-suite paths before every plane prefix, "
            "so a test-only push produces an empty ChangeSet and the deployer fast-forwards it. "
            "That is safe only while a file the deployer ignores is also a file no host "
            "receives. A role shipping its own test suite would get a change to deployed code "
            "fast-forwarded and never deployed — silent, and green from every repo-side check."
        ),
        files=lambda: _role_files("tasks", "templates", "handlers"),
        offence=lambda s: [
            f"line {n} ships {name}"
            for n, name in _shipped(SHIPPED_PY, s)
            if _is_test_file(name)
        ],
        count=lambda s: len(_shipped(SHIPPED_PY, s)),
        red=(
            Subject("a.yml", "- ansible.builtin.copy:\n    src: test_deploy.py\n"),
            Subject("b.yml", "data: {{ lookup('file', 'files/conftest.py') }}\n"),
        ),
        green=(
            Subject("a.yml", "- ansible.builtin.copy:\n    src: deploy_logic.py\n"),
            Subject("b.yml", "# src: test_commented.py is not a ship\n"),
        ),
        # 83 ship lines across 15 files when the row landed (2026-10-03).
        min_matches=60,
        must_find=frozenset(
            {
                "ansible/roles/setup/gitops_deploy/tasks/code.yml",
                "ansible/roles/setup/k3s/tasks/health-crons.yml",
                "ansible/roles/k8s/crowdsec/tasks/main.yml",
            }
        ),
    ),
    Census(
        name="no-role-ships-a-markdown-file",
        reason=(
            "`deploy_logic.is_doc` reads every `.md` as prose no playbook applies, and the tick, "
            "the deploy-plane narrowing and the setup-role narrowing all ask it. That is safe "
            "only while a `.md` the deployer skips is also one no host receives. Rename the "
            "shipped file, or give is_doc back a carve-out; this scan matches a literal basename "
            "and the comment above SHIPPED_MD names the glob shapes it cannot see."
        ),
        # `defaults/` and `vars/` too: a list of basenames there ships files no task names.
        files=lambda: _role_files("tasks", "templates", "handlers", "defaults", "vars"),
        offence=lambda s: [
            f"line {n} ships {name}" for n, name in _shipped(SHIPPED_MD, s)
        ],
        red=(
            Subject(
                "a.yml",
                "- name: Ship the runbook\n  ansible.builtin.copy:\n"
                "    src: RUNBOOK.md\n    dest: /opt/example/RUNBOOK.md\n",
            ),
            Subject("b.yml", "example_config_files:\n  - listed.md\n"),
        ),
        green=(
            Subject("a.yml", "# src: COMMENTED.md is not a ship\n"),
            Subject("b.yml", "example_config_files:\n  - settings.yaml\n"),
        ),
        min_matches=500,
        # The `*_files` lists the bare-entry alternative exists for, and the deployer's own role.
        must_find=frozenset(
            {
                "ansible/roles/k8s/home-assistant/defaults/main.yml",
                "ansible/roles/setup/gitops_deploy/tasks/code.yml",
            }
        ),
    ),
    Census(
        name="role-tasks-write-no-home-relative-path",
        reason=(
            "`path: ~/.ssh` in a task with no `become:` of its own, under a play that becomes "
            "root, writes /root/.ssh: the file module expands `~` as the become user and sudo "
            "passes `-H`. Nothing in the task's text says which directory it writes, so the bug "
            "survives every reading of it. Every playbook here becomes root and none means "
            "/root, so name the directory absolutely (`/home/{{ sys_user }}/.ssh`)."
        ),
        files=lambda: [
            rel
            for rel in tracked("ansible/roles/*/tasks/*.yml")
            if len(parts := rel.split("/")) == 6 and parts[4] == "tasks"
        ],
        offence=_home_relative_paths,
        red=(
            Subject(
                "a.yml",
                "- name: Set SSH directory permissions\n"
                "  ansible.builtin.file:\n    path: ~/.ssh\n",
            ),
            Subject(
                "b.yml",
                "- block:\n    - ansible.builtin.copy:\n        src: ~/x\n        dest: ~/y\n",
            ),
        ),
        green=(
            Subject(
                "a.yml",
                "- name: Harden\n  ansible.builtin.file:\n"
                "    path: /home/{{ sys_user }}/.ssh\n",
            ),
            Subject(
                "b.yml",
                "- ansible.builtin.copy:\n    src: ~/control-side\n    dest: /x\n",
            ),
        ),
        min_matches=100,
        must_find=frozenset({"ansible/roles/setup/initial_setup/tasks/access.yml"}),
    ),
)

_IDS = [row.name for row in ROWS]


@pytest.mark.parametrize("row", ROWS, ids=_IDS)
def test_census_row_holds_on_the_tree(row: Census):
    problems = check(row)
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("row", ROWS, ids=_IDS)
def test_census_row_flags_its_red_subjects_and_passes_its_green_ones(row: Census):
    assert not proof_problems(row)
