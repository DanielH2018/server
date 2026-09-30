#!/usr/bin/env python3
"""No role ships a `.md` to a host, which is what lets one rule answer for every `.md`.

`deploy_logic.is_doc` reads every `.md` as prose no playbook applies, and the tick, the
deploy-plane narrowing (`narrow_paths.is_prose`) and the setup-role narrowing
(`narrow_setup._reaches_no_host`) all ask it. Before #2810 the two narrowings carved out a
`.md` under a role's `files/` or `templates/` directory as shippable, because a task CAN copy
or render one — so the same file was prose to the tick and role content to the derivation.

Deciding it once turns that carve-out into the invariant this guard holds: a `.md` the deployer
skips must also be a `.md` no host receives. A role that started shipping one would otherwise
get a change to a deployed file fast-forwarded and never applied — silent, and green from
every other repo-side check. `test_no_role_ships_a_test_file.py` holds the same invariant for
a `.py`, for the same reason.

Run: uv run pytest ansible/tests/deploy/test_no_role_ships_a_markdown_file.py
"""

import re
from pathlib import Path

from _helpers import ANSIBLE

# WHAT THIS SCAN DOES NOT SEE: a ship that names no basename. `with_fileglob`/`fileglob` over a
# directory, a directory-shaped `src:` in a copy task, and `unarchive`/`synchronize` of a tree all
# put files on a host without a literal name to match. None of them ships a `.md` today — the two
# globs in the tree take `*.json` and `*.pub`, the three `unarchive` tasks take remote tarballs,
# and no task carries a directory `src:` — so the hole is latent rather than live. A `.md` added
# under one of those globs would pass this guard and be skipped by the deployer, which is the one
# failure mode to check when a role starts globbing a directory that holds prose.
#
# The three shapes a role uses to put a named file on a host: a `src:`/`dest:` in a copy or
# template task, the `lookup('file', ...)` a k8s ConfigMap inlines a file with, and a bare YAML
# list entry, which is how `home-assistant`'s four `*_files` lists in `defaults/main.yml` name
# what its ConfigMap ships. A `.md` mentioned in prose is not a ship, and comments are stripped
# before matching, so a docstring pointing at `docs/foo.md` does not register.
_SHIP = re.compile(
    r"""(?:src|dest)\s*:\s*['"]?\S*?(?P<a>[\w.-]+\.md)"""
    r"""|lookup\(\s*['"]file['"]\s*,\s*['"]?\S*?(?P<b>[\w.-]+\.md)"""
    r"""|^\s*-\s+['"]?(?P<c>[\w.-]+\.md)['"]?\s*$""",
    re.MULTILINE,
)

# Where a role names a file it ships. `defaults/` and `vars/` are read as well as the task and
# template directories, because a list of basenames in `defaults/main.yml` ships files that no
# task or template names literally.
_ROLE_DIRS = ("tasks", "templates", "handlers", "defaults", "vars")

# `.md` files that live under a role's `files/` or `templates/` directory today. They are the
# reason the carve-out existed; each is documentation beside the data it describes, named by no
# list. The census below must still find them — a glob that stopped matching would pass every
# check here vacuously.
KNOWN_MARKDOWN_UNDER_FILES = frozenset(
    {
        "roles/k8s/configarr/files/baseline/README.md",
        "roles/k8s/home-assistant/files/automations/README.md",
        "roles/k8s/home-assistant/files/scripts/README.md",
    }
)


def _markdown_under_shipped_dirs(root: Path) -> set[str]:
    """Every `.md` under a role's `files/` or `templates/` directory, relative to `root`."""
    return {
        str(path.relative_to(root))
        for directory in ("files", "templates")
        for path in root.glob(f"roles/*/*/{directory}/**/*.md")
        if path.is_file()
    }


def _shipped_markdown_names(root: Path) -> list[tuple[str, str]]:
    """(file:line, markdown basename) for every `.md` a role names as a file to ship."""
    found = []
    candidates = [
        path
        for directory in _ROLE_DIRS
        for path in root.glob(f"roles/*/*/{directory}/**/*")
    ]
    for path in sorted(candidates):
        if not path.is_file():
            continue
        for i, line in enumerate(path.read_text(errors="ignore").splitlines(), 1):
            body = line.split("#", 1)[0]
            for match in _SHIP.finditer(body):
                name = match.group("a") or match.group("b") or match.group("c")
                if name:
                    found.append((f"{path.relative_to(root)}:{i}", name))
    return found


def test_the_census_finds_the_markdown_files_under_files_directories() -> None:
    """Guard the guard: the glob still reaches the files whose status is being asserted."""
    found = _markdown_under_shipped_dirs(ANSIBLE)
    missing = KNOWN_MARKDOWN_UNDER_FILES - found
    assert not missing, (
        "the census no longer finds "
        + ", ".join(sorted(missing))
        + " — the glob broke, so every check in this file passes over an empty tree"
    )


def test_no_role_ships_a_markdown_file() -> None:
    """The invariant `deploy_logic.is_doc` asserts rather than derives."""
    offenders = [
        f"{where} ships {name}" for where, name in _shipped_markdown_names(ANSIBLE)
    ]
    assert not offenders, (
        "a role ships a .md to a host, so deploy_logic.is_doc would read a change to a "
        "deployed file as prose — the deployer fast-forwards it and never applies it. Either "
        "rename the shipped file or give is_doc back a carve-out (this scan matches a literal "
        "basename; the header names the glob shapes it cannot see): "
        + "; ".join(offenders)
    )


def test_a_task_that_copies_a_markdown_file_is_flagged(tmp_path: Path) -> None:
    """The rejecting half: the scan over a tree that does ship one must find it."""
    role = tmp_path / "roles" / "k8s" / "example"
    (role / "tasks").mkdir(parents=True)
    (role / "tasks" / "main.yml").write_text(
        "- name: Ship the runbook\n"
        "  ansible.builtin.copy:\n"
        "    src: RUNBOOK.md\n"
        "    dest: /opt/example/RUNBOOK.md\n"
        "# src: COMMENTED.md is not a ship\n"
    )
    (role / "defaults").mkdir()
    (role / "defaults" / "main.yml").write_text(
        "example_config_files:\n  - listed.md\n  - settings.yaml\n"
    )
    found = dict(_shipped_markdown_names(tmp_path))
    assert set(found.values()) == {"RUNBOOK.md", "listed.md"}, found
