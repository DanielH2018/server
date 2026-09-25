"""Every play sets `PYTHONDONTWRITEBYTECODE`, so an escalated task leaves no root-owned bytecode.

A play run from a Claude worktree executes its modules with that tree's `.venv/bin/python`
(`uv run` puts it on PATH, and `ansible.cfg`'s `interpreter_python = auto_silent` discovers
it). A task with `become: true` therefore imports from the worktree's site-packages AS ROOT,
and CPython writes the `__pycache__` for anything not already compiled with root's ownership.

The session that created the tree cannot then remove it. Measured 2026-09-24 (#2456), after
`k3s-bringup.yml --tags node-dns` was applied by hand from a worktree:

    .venv/lib/python3.14/site-packages/cryptography/x509/__pycache__   root:root 750

`git worktree remove` unregistered the tree and failed with `Directory not empty`; the
recursive delete that followed failed with `Permission denied`. Only a delete run under sudo
clears it, and sessions here have no sudo. `prune_worktrees.py` and `ExitWorktree` hit the
same wall.

`PYTHONDONTWRITEBYTECODE=1` in the play's `environment:` stops the write at the source.
Ansible renders that key into the module command string, and
`ActionBase._low_level_execute_command` passes THAT string to `build_become_command`, so the
variable sits inside the shell command sudo runs rather than in sudo's own environment —
which `env_reset` would have discarded. A task-level `environment:` merges with the play's
rather than replacing it, so `drain_backup_prefix.yml`'s B2 keys still reach their task.

The alternative the issue offered — pointing `ansible_python_interpreter` outside the tree —
is not available here. The repo's venv is load-bearing for module execution: `community.docker`
needs `requests` and `docker`, and the k8s modules need the Kubernetes client, none of which
the host's python3.12 carries.
"""

from pathlib import Path

from lib import yaml_fast
from _helpers import REPO as _REPO


_ENV_KEY = "PYTHONDONTWRITEBYTECODE"
_PLAYBOOK_DIR = _REPO / "ansible"

# The playbooks known when this guard was written. Asserted below so that a rename or a move
# fails loudly here rather than leaving the guard matching nothing and passing vacuously.
_KNOWN = frozenset(
    {
        "bootstrap.yml",
        "deploy.yml",
        "drain_backup_prefix.yml",
        "drop_migrated_backup_chain.yml",
        "drop_seed_backups.yml",
        "initial_setup.yml",
        "k3s-bringup.yml",
        "k3s-storage-smoke.yml",
        "migrate_volume_block_size.yml",
        "preflight.yml",
        "seed_volume_backup.yml",
    }
)


def _plays() -> list[tuple[Path, int, dict]]:
    """(path, zero-based index within the file, play) for every play under ansible/.

    A play is a top-level mapping carrying `hosts`. Entries without it — an
    `import_playbook`, say — are not plays and carry no `environment:`, so they are skipped
    individually rather than disqualifying the whole file. `requirements.yml` (a galaxy
    requirements file) and `secret_rotation.yml` (the rotation registry) are both top-level
    mappings, so neither reaches the loop body.
    """
    found: list[tuple[Path, int, dict]] = []
    for path in sorted(_PLAYBOOK_DIR.glob("*.yml")):
        doc = yaml_fast.safe_load(path.read_text())
        if not isinstance(doc, list):
            continue
        plays = [p for p in doc if isinstance(p, dict) and "hosts" in p]
        found.extend((path, i, play) for i, play in enumerate(plays))
    return found


def _plays_missing_the_key(plays: list[tuple[Path, int, dict]]) -> list[str]:
    """The plays whose `environment:` does not set the key, named for the failure message."""
    return [
        f"{path.relative_to(_REPO)} play {i} ({play.get('name', '<unnamed>')})"
        for path, i, play in plays
        if (play.get("environment") or {}).get(_ENV_KEY) != "1"
    ]


def test_every_play_disables_bytecode_writes() -> None:
    missing = _plays_missing_the_key(_plays())
    assert not missing, (
        f"these plays do not set {_ENV_KEY} in their `environment:`: {missing}. "
        "A `become` task in them writes root-owned bytecode into the worktree's .venv, "
        "and the worktree can then only be removed under sudo. Add "
        f'`environment:` / `{_ENV_KEY}: "1"` to the play.'
    )


def test_a_play_without_the_key_is_flagged() -> None:
    """Reject half. A play with no `environment:` at all, and one that sets other keys."""
    bare = (_REPO / "ansible/made-up.yml", 0, {"name": "Bare", "hosts": "localhost"})
    other = (
        _REPO / "ansible/made-up.yml",
        1,
        {"name": "Other keys", "hosts": "localhost", "environment": {"B2_KEY_ID": "x"}},
    )
    assert _plays_missing_the_key([bare, other]) == [
        "ansible/made-up.yml play 0 (Bare)",
        "ansible/made-up.yml play 1 (Other keys)",
    ]


def test_a_play_with_the_key_is_clean() -> None:
    """Accept half, against the same synthetic shape the reject half uses."""
    play = (
        _REPO / "ansible/made-up.yml",
        0,
        {"hosts": "localhost", "environment": {_ENV_KEY: "1", "B2_KEY_ID": "x"}},
    )
    assert _plays_missing_the_key([play]) == []


def test_the_guard_still_matches_the_known_playbooks() -> None:
    """A guard that matches nothing passes for the wrong reason."""
    found = {path.name for path, _, _ in _plays()}
    assert _KNOWN <= found, (
        f"expected playbooks not found: {sorted(_KNOWN - found)}. Either they moved, or "
        "_plays() stopped recognising the playbook shape — fix the matcher, or update "
        "_KNOWN if the playbook is genuinely gone."
    )
