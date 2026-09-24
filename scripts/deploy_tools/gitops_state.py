#!/usr/bin/env python3
"""Operate on the GitOps deployer's own state markers, from the deploy host's shell.

Three subcommands. `clear-contention` removes `/var/lib/gitops-deploy/contention_since`, the
marker the deployer writes while consecutive ticks defer on one busy service lock (issue
#1847); the tick clears it itself on its next run that is not deferred, so this is for a
marker an operator wants gone now, after ending the holder. `clear-manual-plane <role>` drops a role's line from
`/var/lib/gitops-deploy/manual_plane`, the marker the deployer writes when a range carries a
setup role no playbook it runs can apply — `k3s` (applied by `k3s-bringup.yml`) or `common`
(applied by no playbook at all). The tick fast-forwards past such a range rather than parking
it, so the marker is what says the apply is still owed: monitor-bridge pages once the oldest
pending role is six hours old, and `land.sh` prints the same clear command.

**The apply comes first, this second.** Clearing a role nobody applied silences the only
durable signal that it is unapplied, which is the state the marker exists to make visible.
So every `clear-manual-plane` run writes one journal line, `logger -t gitops-state`, naming
the role, the line it dropped and who ran it: `journalctl -t gitops-state` is where a clear
with no apply behind it leaves its trace (#2022: `k3s` was cleared by hand on 2026-09-18
with the apply still owed, and the marker's own truncation recorded nothing).
`clear-contention` writes no such line on purpose: it silences no page, and the tick
rewrites that marker itself on its next undeferred run.

`clear-k8s-deferred <service>` is the same shape one plane over. `/var/lib/gitops-deploy/
k8s_deferred` holds one line per promoted image bump a BROAD tick fast-forwarded and then
deferred for lack of budget (#2449): the range is merged, so no later tick's `local..origin`
carries the bump and the defer-and-alert post names it exactly once. monitor-bridge pages once
the oldest line is six hours old. The deployer clears a line itself on any tick that deploys
the service; this command is for the `./scripts/deploy.sh` an operator ran, which the deployer
cannot see. The apply comes first here too, and the clear writes the same journal line under
`event=clear-k8s-deferred`.

This is not a path the deployer takes. Its own reverse is
`DeployerState.clear_manual_plane_applied`, which fires when a tick applies the role's real
playbook and tag — unreachable today, since it runs neither of the two playbooks in question.

WHERE IT RUNS. `/var/lib/gitops-deploy` is 0750 and owned by `sys_user` (`ubuntu` on
daniel-box), so the deploy user's own shell writes it directly and any other user needs
`sudo -u ubuntu`. A directory this uid cannot write is reported as that, not as a traceback.

The rewrite takes `/var/lock/server-git-tree.lock`, the lock a tick already holds, so the two
cannot interleave over the same file. It waits seconds rather than minutes and then refuses:
re-run it once the deploy or tick finishes.

Run: uv run pytest scripts/deploy_tools/tests/test_gitops_state.py
"""

import argparse
import contextlib
import fcntl
import getpass
import os
import subprocess
import sys
import time
from collections.abc import Callable

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
from lib.repo_paths import GITOPS_DEPLOY_FILES, HOST_LIB_FILES

# The deployer's own modules, so this reads and rewrites the marker through the code that
# writes it rather than through a second copy of the format. `deploy_state` reaches
# `host_lib` for its atomic write, which is why both directories go on the path — unlike the
# tools that import `deploy_logic`, which must stay on `files/` alone.
_sys.path.insert(0, str(GITOPS_DEPLOY_FILES))
_sys.path.insert(0, str(HOST_LIB_FILES))

from deploy_changes import setup_role_tag
from deploy_locks import TREE_LOCK
from deploy_state import STATE_DIR, DeployerState, ManualPlaneEntry
from gitops_markers import manual_plane_clear_cmd, maximal_apply_warning

# Seconds to wait for it. Every other waiter on this lock waits 3000 (the census in the
# deployer's test_gitops_deploy_timeout_budgets.py), because those are unattended jobs that
# must not skip their run. This is an operator at a prompt, and a tick can hold the lock for
# most of an hour, so waiting it out would read as a hang. Refusing is the better answer: the
# clear changes one line, is idempotent, and costs nothing to re-run.
LOCK_WAIT_S = 5.0


class LockBusy(Exception):
    """The tree lock stayed held for the whole wait, so nothing was read or written."""


class LockUnavailable(Exception):
    """The lock FILE could not be opened at all — a wrong mode, or a missing directory.

    Separate from `LockBusy`, and separate from the state directory's own `PermissionError`:
    all three exit 1, and an operator needs to know which of the two paths they cannot reach.

    Attributes:
        args: the lock path, then the `OSError` that explains it.
    """


@contextlib.contextmanager
def tree_lock(path: str, wait_s: float | None = None):
    """Hold the tree lock across a read-modify-write of the marker, or raise `LockBusy`.

    `DeployerState.record_manual_plane` reads every line, appends one and writes the file
    back; so does `clear_manual_plane`. Interleaved, the loser's write drops the winner's
    line — a role recorded and then silently lost, or a cleared role reappearing. The tick
    already runs under this lock, so taking it here is what makes the pair safe.

    Args:
      wait_s: how long to wait for the lock. None reads `LOCK_WAIT_S` at call time, which is
        what lets a test shorten the wait rather than sleep through it.

    Raises:
      LockBusy: the lock was held for the whole wait.
      LockUnavailable: the lock file could not be opened. Raised rather than left as a bare
        OSError so the caller cannot attribute it to the state directory, which has its own
        PermissionError and its own remediation.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CREAT, 0o666)
    except OSError as exc:
        raise LockUnavailable(path, exc) from exc
    try:
        deadline = time.monotonic() + (LOCK_WAIT_S if wait_s is None else wait_s)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise LockBusy(path) from exc
                time.sleep(0.1)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def marker_key(role: str) -> str:
    """The `manual_plane` line key for a role, which is the `--tags` value that selects it."""
    return setup_role_tag(role)


# The syslog tag the journal line carries: `journalctl -t gitops-state` reads it back, and
# the Alloy shipper tails it into Loki with the rest of /var/log/syslog.
JOURNAL_TAG = "gitops-state"

# What `clear_manual_plane` calls with the role, the line it dropped (None when it kept the
# line or found none) and the tags still pending after the clear.
Journal = Callable[[str, "ManualPlaneEntry | None", frozenset], None]


def operator() -> str:
    """Who is running this, through `sudo -u ubuntu` when that is how they reached the uid."""
    return os.environ.get("SUDO_USER") or getpass.getuser()


def journal_clear(
    role: str,
    dropped: ManualPlaneEntry | None,
    remaining: frozenset[str] = frozenset(),
    run: Callable[..., object] = subprocess.run,
    event: str = "clear-manual-plane",
) -> None:
    """Write the one line that says an operator cleared `role`, who, and from where.

    logfmt like `deploy_playbook.annotate`, and fire-and-forget the same way:
    `logger` missing, or the syslog socket refusing, changes nothing about the exit code. The
    clear already happened by the time this runs; a line saying so must not make it read as
    failed. `dropped` is the marker line the clear removed, or None for a no-op clear, which
    is still evidence that someone tried. Its origin SHA and playbook are what an
    investigator needs to match the clear against the apply that did or did not follow.

    Args:
      run: what executes `logger`; `subprocess.run` outside a test.
      event: which clear this was. `journal_clear_k8s_deferred` passes the deferred-bump one,
        so `journalctl -t gitops-state` distinguishes the two markers an operator can clear.
    """
    fields = [
        f"event={event}",
        f"role={role}",
        f"cleared={'true' if dropped else 'false'}",
        f"user={operator()}",
        f"cwd={os.getcwd()}",
    ]
    if remaining:
        # A narrowed clear that kept the line. Without this field the journal would read like
        # a no-op clear, when what happened is that the role is STILL pending for other tags.
        fields.append(f"still_pending={','.join(sorted(remaining))}")
    if dropped:
        fields.append(f"origin={dropped.origin}")
        fields.append(f"playbook={dropped.playbook}")
        fields.append(f"pending_since={dropped.at:.0f}")
    try:
        run(
            ["logger", "-t", JOURNAL_TAG, " ".join(fields)],
            check=False,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except OSError, subprocess.SubprocessError:
        pass


def journal_clear_k8s_deferred(
    service: str,
    dropped: ManualPlaneEntry | None,
    remaining: frozenset[str] = frozenset(),
    run: Callable[..., object] = subprocess.run,
) -> None:
    """`journal_clear` under the deferred-bump event name, for `clear_k8s_deferred`."""
    journal_clear(service, dropped, remaining, run, event="clear-k8s-deferred")


def clear_manual_plane(
    state: DeployerState,
    role: str,
    lock_path: str | None = None,
    lock_wait_s: float | None = None,
    journal: Journal | None = None,
    applied: frozenset[str] = frozenset(),
) -> int:
    """Drop `role`'s pending line. Exit 0 whether or not there was one to drop.

    Args:
      lock_path: the tree lock to serialise the rewrite against. None reads
        `deploy_locks.TREE_LOCK`, the path every writer of this host's checkout takes.
      lock_wait_s: how long to wait for it. None reads `LOCK_WAIT_S`.
      journal: what records the clear, called once with the role, the line it dropped (None
        when it kept the line or found none) and the tags still pending. None means
        `journal_clear`, the real `logger` line.
      applied: the tags the operator actually ran, from `--applied`. Empty means a whole-role
        apply, which clears the line however the row has grown; a narrowed apply drops only
        its own tags and leaves the line standing for anything a later range added (#2349).
        A narrowed apply against an empty or missing row keeps the line: that row means the
        whole role, which a narrowed apply does not cover.
    """
    key = marker_key(role)
    try:
        with tree_lock(TREE_LOCK if lock_path is None else lock_path, lock_wait_s):
            # Read the line before dropping it: the journal names what was cleared, not
            # just that something was. Same lock, so it is the line the clear removes.
            dropped = next(
                (e for e in state.manual_plane_pending() if e.role == key), None
            )
            if applied and dropped is None:
                remaining, cleared = None, False
            elif applied:
                remaining = state.clear_manual_plane_tags_applied(key, applied)
                cleared = remaining is None
            else:
                remaining = None
                cleared = state.clear_manual_plane(key)
    except LockBusy as busy:
        print(
            f"{busy.args[0]} is held — a deploy or a gitops tick is running. Nothing was "
            "changed; re-run this when it finishes.",
            file=sys.stderr,
        )
        return 1
    except LockUnavailable as bad_lock:
        path, exc = bad_lock.args
        print(
            f"cannot open the tree lock {path}: {exc}. Nothing was changed — this command "
            "serialises against that lock and will not write the marker without it.",
            file=sys.stderr,
        )
        return 1
    except PermissionError:
        print(
            f"cannot write {state.path('manual_plane')} as this user — the state directory "
            "is owned by the deploy user; retry with `sudo -u ubuntu`",
            file=sys.stderr,
        )
        return 1
    # After the lock is released and only once the rewrite happened: a refusal above writes
    # no line, because a line claiming a clear that never happened is worse than none.
    (journal_clear if journal is None else journal)(
        key, dropped if cleared else None, remaining or frozenset()
    )
    if remaining == frozenset({key}):
        # The row is empty or missing, so the WHOLE role is pending: a later range's
        # derivation refused after this command was printed. No narrowed apply covers that.
        # The whole-role apply this sends the operator to is the one `maximal_apply_warning`
        # exists for (#2345): every other surface printing that command carries the warning,
        # and this one printed it bare.
        warning = maximal_apply_warning(key, frozenset({key}))
        print(
            f"kept {role} in {state.path('manual_plane')}: its row in "
            f"{state.path('manual_plane_tags')} is empty or missing, so the whole role is "
            f"pending, not just {','.join(sorted(applied))}. Apply the whole role"
            + (f" (WARNING: {warning})" if warning else "")
            + f", then clear it without --applied: `{manual_plane_clear_cmd(key)}`"
        )
        return 0
    if remaining:
        print(
            f"cleared {','.join(sorted(applied))} from {role}'s row in "
            f"{state.path('manual_plane_tags')}; {role} is STILL pending for "
            f"{','.join(sorted(remaining))} — a later range added it, so apply that too"
        )
        return 0
    if not cleared:
        print(
            f"{role} is not pending in {state.path('manual_plane')} — nothing to clear"
        )
        return 0
    print(f"cleared {role} from {state.path('manual_plane')}")
    return 0


def clear_k8s_deferred(
    state: DeployerState,
    service: str,
    lock_path: str | None = None,
    lock_wait_s: float | None = None,
    journal: Journal | None = None,
) -> int:
    """Drop `service`'s deferred-bump line. Exit 0 whether or not there was one to drop.

    **The deploy comes first, this second**, for the reason `clear_manual_plane` states: the
    marker is the only durable signal that a bump a broad tick merged is still unapplied, and
    clearing it without deploying silences that. The deployer clears the line itself whenever a
    tick deploys the service; this is for the hand deploy it cannot see.

    Args:
      lock_path: the tree lock to serialise the rewrite against. None reads `TREE_LOCK`.
      lock_wait_s: how long to wait for it. None reads `LOCK_WAIT_S`.
      journal: what records the clear, called with the service, the line it dropped and an
        empty remaining set. None means `journal_clear_k8s_deferred`.
    """
    try:
        with tree_lock(TREE_LOCK if lock_path is None else lock_path, lock_wait_s):
            dropped = next(
                (e for e in state.k8s_deferred_pending() if e.service == service), None
            )
            cleared = bool(state.clear_k8s_deferred({service}))
    except LockBusy as busy:
        print(
            f"{busy.args[0]} is held — a deploy or a gitops tick is running. Nothing was "
            "changed; re-run this when it finishes.",
            file=sys.stderr,
        )
        return 1
    except LockUnavailable as bad_lock:
        path, exc = bad_lock.args
        print(
            f"cannot open the tree lock {path}: {exc}. Nothing was changed — this command "
            "serialises against that lock and will not write the marker without it.",
            file=sys.stderr,
        )
        return 1
    except PermissionError:
        print(
            f"cannot write {state.path('k8s_deferred')} as this user — the state directory "
            "is owned by the deploy user; retry with `sudo -u ubuntu`",
            file=sys.stderr,
        )
        return 1
    (journal_clear_k8s_deferred if journal is None else journal)(
        service,
        # The two markers keep different line shapes, and the journal only needs the SHA the
        # clear was owed against. `ManualPlaneEntry` is what `journal_clear` reads, so the
        # deferred entry is rendered into one rather than given a second formatter.
        ManualPlaneEntry(dropped.origin, "ansible/deploy.yml", service, dropped.at)
        if cleared and dropped
        else None,
        frozenset(),
    )
    if not cleared:
        print(
            f"{service} is not pending in {state.path('k8s_deferred')} — nothing to clear"
        )
        return 0
    print(f"cleared {service} from {state.path('k8s_deferred')}")
    return 0


def clear_contention(
    state: DeployerState,
    lock_path: str | None = None,
    lock_wait_s: float | None = None,
) -> int:
    """Remove the `contention_since` marker. Exit 0 whether or not there was one.

    Serialised against the tree lock exactly as `clear_manual_plane` is, and refused the same
    way while a tick holds it — a tick mid-defer is about to rewrite this marker.
    """
    try:
        with tree_lock(TREE_LOCK if lock_path is None else lock_path, lock_wait_s):
            cleared = state.clear_contention()
    except LockBusy as busy:
        print(
            f"{busy.args[0]} is held — a deploy or a gitops tick is running. Nothing was "
            "changed; re-run this when it finishes.",
            file=sys.stderr,
        )
        return 1
    except LockUnavailable as bad_lock:
        path, exc = bad_lock.args
        print(
            f"cannot open the tree lock {path}: {exc}. Nothing was changed — this command "
            "serialises against that lock and will not write the marker without it.",
            file=sys.stderr,
        )
        return 1
    except PermissionError:
        print(
            f"cannot write {state.path('contention')} as this user — the state directory "
            "is owned by the deploy user; retry with `sudo -u ubuntu`",
            file=sys.stderr,
        )
        return 1
    if not cleared:
        print(f"no contention streak in {state.path('contention')} — nothing to clear")
        return 0
    print(f"cleared {state.path('contention')}")
    return 0


def main(
    argv: list[str] | None = None,
    lock_path: str | None = None,
    lock_wait_s: float | None = None,
    journal: Journal | None = None,
) -> int:
    """Parse `argv` and run the subcommand it names.

    Args:
      lock_path: the tree lock the rewrite serialises against. None reads `TREE_LOCK`; a test
        passes its own, because taking the host's real lock would block a running deploy.
      lock_wait_s: how long to wait for it. None reads `LOCK_WAIT_S`.
      journal: what records a `clear-manual-plane`. None reads `journal_clear`; a test passes
        its own, because a real `logger` line from a test reads as an operator's clear.
    """
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--state-dir",
        default=STATE_DIR,
        help=f"the deployer's state directory (default: {STATE_DIR})",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    clear = sub.add_parser(
        "clear-manual-plane",
        help="drop one setup role's pending line, AFTER applying it by hand",
    )
    clear.add_argument("role", help="the setup role, e.g. k3s or common")
    clear.add_argument(
        "--applied",
        default=None,
        help=(
            "the comma-separated --tags value you actually ran. Omit it after a whole-role "
            "apply; pass it after a narrowed one, so a tag a later range added to the row "
            "stays pending instead of being cleared with yours"
        ),
    )
    sub.add_parser(
        "clear-contention",
        help="drop the busy-service-lock streak marker, AFTER ending the lock's holder",
    )
    deferred = sub.add_parser(
        "clear-k8s-deferred",
        help="drop one deferred image bump's line, AFTER deploying that service",
    )
    deferred.add_argument("service", help="the k8s service, e.g. sonarr")
    args = parser.parse_args(argv)
    state = DeployerState(args.state_dir)
    if args.command == "clear-contention":
        return clear_contention(state, lock_path, lock_wait_s)
    if args.command == "clear-k8s-deferred":
        return clear_k8s_deferred(state, args.service, lock_path, lock_wait_s, journal)
    if args.command != "clear-manual-plane":
        # argparse refuses any other value, so this catches a subcommand added to the parser
        # and not to this dispatch — which would otherwise run the clear with its arguments.
        parser.error(f"no handler for {args.command}")
    applied = frozenset(t.strip() for t in (args.applied or "").split(",") if t.strip())
    if args.applied is not None and not applied:
        # `--applied ""` is what `--applied "$TAGS"` sends with TAGS unset, and an empty set
        # means a WHOLE-role apply here — so it would clear a line that is still pending.
        parser.error(
            "--applied names no tag. Omit it after a whole-role apply; pass the tags you "
            "actually ran after a narrowed one."
        )
    return clear_manual_plane(
        state, args.role, lock_path, lock_wait_s, journal, applied
    )


if __name__ == "__main__":
    sys.exit(main())
