#!/usr/bin/env python3
"""Apply a `manual_plane` line of the deployer's `owed` ledger, then clear it, in one step.

It runs exactly the commands every surface prints for the line (`manual_plane_remedies`), from
a checkout at origin/master, and refuses a line it cannot trust or that needs the operator: a
field no role, tag or host spells, a playbook or host outside the routing and the inventory, a
role applied by hand, and an apply that arms more than the change (#6881). The clear is
`gitops_state.clear_manual_plane`, so the journal line and the tree lock are the same ones a
hand `clear-owed` takes.

Typical usage example:

    uv run python scripts/deploy_tools/owed_run.py k3s --dry-run
    uv run python scripts/deploy_tools/owed_run.py k3s

Run: uv run pytest scripts/deploy_tools/tests/test_owed_run.py
"""

import argparse
import re
import subprocess
import sys
import time
from collections.abc import Callable

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
from deploy_tools.gitops_state import Journal, clear_manual_plane, marker_key
from deploy_tools.setup_routing import PLAYBOOKS
from lib.ansible_inventory import host_names
from lib.cli_help import ArgumentParser
from lib.deployer_park import ManualPlaneRemedy, manual_plane_remedies
from lib.git import git
from lib.repo_paths import REPO

from deployer_lib.deploy_state import STATE_DIR, DeployerState
from gitops_markers import on_host

# What a role, tag or host on a ledger line may spell before this puts it in a shell command.
# The ledger is a file the deploy user can write and this is the command auto mode allows, so
# a line is data to check, never a command to trust.
SAFE_WORD = re.compile(r"[A-Za-z0-9_.:-]+")
# What `agent_join_cmds` and `agent_pin_cmds` print for a line that names no agent host.
AGENT_HOST_PLACEHOLDER = "<agent host>"


def _line_refusal(entry, remedy: ManualPlaneRemedy) -> str | None:
    """Why this line's commands must not run, or None when they may."""
    hosts = set(entry.apply_hosts()) | set(entry.agent_hosts)
    words = {entry.role, *remedy.tags, *hosts}
    if bad := sorted(w for w in words if not SAFE_WORD.fullmatch(w)):
        return f"the line carries a value no role, tag or host spells: {bad}"
    if remedy.by_hand:
        return f"no recorded command applies it: {remedy.by_hand}"
    if entry.playbook not in PLAYBOOKS:
        return f"its playbook {entry.playbook!r} is not one the routing records"
    if unknown := sorted(hosts - set(host_names())):
        return f"it names hosts the inventory does not: {unknown}"
    if remedy.warning:
        return f"the apply arms more than the change, so the operator runs it: {remedy.warning}"
    commands = [remedy.apply, *remedy.agents, *(cmd for _, cmd in remedy.pins)]
    if any(AGENT_HOST_PLACEHOLDER in c for c in commands):
        return "the line names no agent host, so a join or pin command has no target"
    if not any(commands):
        return "the line yields no command to run"
    return None


def _checkout_is_origin_master(repo: str) -> str | None:
    """None when `repo`'s HEAD is origin/master after a fetch, else why not."""
    try:
        fetch = git(
            "fetch", "-q", "origin", "master", cwd=repo, check=False, timeout=120
        )
        if fetch.returncode:
            return f"git fetch failed: {fetch.stderr.strip()}"
        shas = [
            git("rev-parse", ref, cwd=repo, check=False, timeout=30).stdout.strip()
            for ref in ("HEAD", "origin/master")
        ]
    except subprocess.TimeoutExpired as exc:
        return f"`{' '.join(exc.cmd)}` did not finish in {exc.timeout:.0f}s"
    return (
        None
        if shas[0] == shas[1]
        else f"HEAD {shas[0][:10]} is not origin/master {shas[1][:10]}"
    )


def run_owed(
    state: DeployerState,
    role: str,
    dry_run: bool = False,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    checkout_check: Callable[[str], str | None] = _checkout_is_origin_master,
    repo: str = str(REPO),
    now: float | None = None,
    lock_path: str | None = None,
    lock_wait_s: float | None = None,
    journal: Journal | None = None,
) -> int:
    """Run exactly the commands `role`'s `manual_plane` line owes, then clear the line.

    The commands are the ones every surface prints for the line (`manual_plane_remedies`):
    the server play, already wrapped in `ssh <host>` for a host that applies its own roles,
    then each agent join, then each agent pin on the host it must run on. Each runs only if
    the one before it exited 0, and the line clears only once all of them have, through
    `clear_manual_plane` with the tags they selected. No lock is held across the playbooks:
    a tick can hold the tree lock for most of an hour, and the clear takes it itself.

    Args:
      dry_run: print the commands and change nothing.
      run: what executes each command; `subprocess.run` outside a test.
      checkout_check: returns why `repo` is not at origin/master, or None.
      repo: the checkout the commands run from, so its relative playbook paths resolve.
      now: the clock the remedies read, for a test.
      lock_path, lock_wait_s, journal: passed to `clear_manual_plane`.

    Returns 0 after a clear or a dry run, 1 on a refusal, a role that is not pending, or a
    failed command, which leaves the line in place.
    """
    owed = state.read("owed")
    entries = {e.role: e for e in state.manual_plane_pending()}
    key = marker_key(role, entries)
    entry = entries.get(key)
    remedy = next(
        (
            r
            for r in manual_plane_remedies(owed, time.time() if now is None else now)
            if r.role == key
        ),
        None,
    )
    if entry is None or remedy is None:
        print(
            f"{role} is not pending in {state.path('owed')} — nothing to run",
            file=sys.stderr,
        )
        return 1
    if refusal := _line_refusal(entry, remedy):
        print(f"refused {key}: {refusal}. Nothing was run.", file=sys.stderr)
        return 1
    commands = [
        *([remedy.apply] if remedy.apply else []),
        *remedy.agents,
        *(on_host(cmd, host) for host, cmd in remedy.pins),
    ]
    if dry_run:
        for cmd in commands:
            print(cmd)
        print(f"then: clear {key} for {','.join(sorted(remedy.tags))}")
        return 0
    if stale := checkout_check(repo):
        print(f"refused {key}: {stale}. Nothing was run.", file=sys.stderr)
        return 1
    for cmd in commands:
        print(f"+ {cmd}", flush=True)
        if (done := run(["bash", "-c", cmd], cwd=repo)).returncode:
            print(
                f"exit {done.returncode}: {key} stays pending in {state.path('owed')}",
                file=sys.stderr,
            )
            return 1
    return clear_manual_plane(state, key, lock_path, lock_wait_s, journal, remedy.tags)


def main(argv: list[str] | None = None) -> int:
    """Parse `argv` and run `role`'s owed commands, or print them under `--dry-run`."""
    parser = ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--state-dir",
        default=STATE_DIR,
        help=f"the deployer's state directory (default: {STATE_DIR})",
    )
    parser.add_argument("role", help="the setup role the manual_plane line names")
    parser.add_argument(
        "--dry-run", action="store_true", help="print the commands and change nothing"
    )
    args = parser.parse_args(argv)
    return run_owed(DeployerState(args.state_dir), args.role, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
