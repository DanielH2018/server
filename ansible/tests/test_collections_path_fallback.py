"""Guard on `collections_path` in ansible.cfg keeping an absolute fallback.

`ansible/collections` is gitignored, so a fresh `.claude/worktrees/<name>` checkout has
none. With a cwd-relative `collections_path` alone, every playbook run from that worktree
resolves the path inside itself, finds nothing, and dies at the secret-load pre_task:

    [ERROR]: couldn't resolve module/action 'community.sops.load_vars'. This often
    indicates a misspelling, missing collection, or incorrect module path.

The message names a module, never the worktree, and the same command works one directory
over in the primary checkout. Appending the primary checkout's absolute path as a second,
lower-priority entry makes a worktree work with no per-worktree install.

Both halves are guarded, because either one alone re-opens a failure:

  * losing the absolute entry brings the worktree breakage back;
  * losing the relative entry, or putting the absolute one first, makes every checkout load
    the PRIMARY checkout's collections — a `requirements.yml` bump would then be deployed
    against the old versions, from the branch that changed them. This governs DEPLOYS only:
    `.claude/hooks/ansible-lint.sh` exports `ANSIBLE_COLLECTIONS_PATH`, and an env var
    overrides ansible.cfg outright, so lint in a worktree already reads the primary
    checkout's collections whatever this value says.
"""

from __future__ import annotations

import configparser
from pathlib import Path

import pytest
from _helpers import REPO

_REPO = REPO
_CFG = _REPO / "ansible.cfg"

_RELATIVE = "ansible/collections"
_ABSOLUTE = "/home/ubuntu/server/ansible/collections"


def _entries(value: str) -> list[str]:
    """Split a `collections_path` value the way Ansible reads it: colon-separated, first wins."""
    return [part.strip() for part in value.split(":") if part.strip()]


def _absolute_fallbacks(value: str) -> list[str]:
    """The entries that survive being resolved from a checkout that has no local copy."""
    return [entry for entry in _entries(value) if Path(entry).is_absolute()]


def _configured_value() -> str:
    parser = configparser.ConfigParser()
    parser.read(_CFG)
    return parser["defaults"]["collections_path"]


def test_configured_collections_path_is_clean():
    """The real ansible.cfg carries a relative entry first and an absolute one after it."""
    entries = _entries(_configured_value())

    assert entries[0] == _RELATIVE, (
        f"collections_path must resolve the LOCAL checkout first, got {entries!r}. "
        "An absolute entry in front makes every worktree load the primary checkout's "
        "collections, hiding a requirements.yml bump from the branch that made it."
    )
    assert _absolute_fallbacks(_configured_value()) == [_ABSOLUTE], (
        f"collections_path needs {_ABSOLUTE} as its fallback, got {entries!r}. "
        "Without it a fresh worktree dies at the secret-load pre_task with "
        "\"couldn't resolve module/action 'community.sops.load_vars'\"."
    )


@pytest.mark.parametrize(
    "value",
    [
        "ansible/collections",
        "ansible/collections:./ansible/collections",
        "  ansible/collections  ",
    ],
)
def test_relative_only_collections_path_is_flagged(value):
    """A relative-only value has no fallback — the state that broke every worktree."""
    assert _absolute_fallbacks(value) == []


def test_absolute_first_is_flagged():
    """An absolute entry in front is caught too: the local checkout would never win."""
    assert _entries(f"{_ABSOLUTE}:{_RELATIVE}")[0] != _RELATIVE
