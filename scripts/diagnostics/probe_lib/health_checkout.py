"""Whether `probe.py health <tag>` runs from a checkout older than the tag's k8s role (#5909).

The resolver in health.py reads a role's workloads from the role's manifests in THIS checkout.
A checkout that predates the role has no `roles/k8s/<tag>/`, so the resolver returns None and
`run_health` guesses a workload named after the tag in the default namespace. For `metallb`,
whose workloads are `metallb-system/controller` and `metallb-system/speaker`, that guess
printed "no Deployment or DaemonSet in this namespace" and exited 1 against a healthy cluster:
the probe ran from a worktree six hours behind the merge that added the role.

The probe observed nothing about the workload in that case, so it is a read that did not
happen (`PROBE_UNREADABLE`), not a verdict. `origin/master` is read from the local ref and
needs no network, so it is only as fresh as the last fetch, which is enough to see a role
that has already merged.

A role that exists here can still be stale (#5912). When `origin/master` changed the role's
files after this checkout's HEAD, the gate would judge the live workload against the old
definition, in either direction. That case is unreadable too, and the fix is the same: run the
probe from a checkout at or after the merge.
"""

import subprocess
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from lib.git import git
from lib.repo_paths import K8S_ROLES, REPO


def role_on_origin(role, roles_dir=K8S_ROLES, repo=REPO):
    """True when `origin/master` carries `roles_dir/role` as a directory, in `repo`'s history.

    False on any failure: no git, no `origin/master` ref, a `roles_dir` outside `repo`. Each of
    those leaves `run_health` on the path it took before this check existed, so the check can
    only add a refusal, never remove a verdict.
    """
    try:
        rel = (_Path(roles_dir) / role).resolve().relative_to(_Path(repo).resolve())
        res = git(
            "cat-file",
            "-t",
            f"origin/master:{rel.as_posix()}",
            cwd=repo,
            check=False,
            timeout=10,
        )
    except OSError, ValueError, subprocess.TimeoutExpired:
        return False
    return res.returncode == 0 and res.stdout.strip() == "tree"


def role_changed_on_origin(role, roles_dir=K8S_ROLES):
    """The newest `origin/master` commit touching `roles_dir/role` that HEAD lacks, or None.

    The repository is the one holding `roles_dir`, so a fixture tree answers for itself. A
    checkout at the merge, or past it with commits of its own, has none. A checkout whose role
    files already match `origin/master` has none either: after a squash merge, the session's
    branch lacks the squash commit but holds the same role. None on any failure, for the same
    reason `role_on_origin` returns False: the check only adds a refusal.
    """
    try:
        res = git(
            "log",
            "-1",
            "--format=%h",
            "HEAD..origin/master",
            "--",
            role,
            cwd=roles_dir,
            check=False,
            timeout=10,
        )
        newer = res.stdout.strip() if res.returncode == 0 else ""
        if not newer:
            return None
        same = git(
            "diff",
            "--quiet",
            "HEAD",
            "origin/master",
            "--",
            role,
            cwd=roles_dir,
            check=False,
            timeout=10,
        )
    except OSError, subprocess.TimeoutExpired:
        return None
    return newer if same.returncode == 1 else None


def stale_checkout_reason(role, roles_dir=K8S_ROLES, on_origin=role_on_origin):
    """Why this checkout cannot judge `role`, or None when it can.

    None when the role directory exists here and `origin/master` has not changed it since this
    checkout's HEAD, or when `origin/master` has no such role either: a Pi tag such as
    `glances` must still reach the guess-the-name path and `--docker`.
    The text carries none of `detach_notify.NOT_APPLICABLE_MARKERS`, so the deploy notifier
    reports the tag as unread rather than as skipped.
    """
    if (_Path(roles_dir) / role).is_dir():
        newer = role_changed_on_origin(role, roles_dir)
        if newer is None:
            return None
        return (
            f"origin/master changed the k8s role {role!r} at {newer}, after this checkout's "
            f"HEAD ({_Path(roles_dir)}), so this checkout holds an old definition of it — no "
            "verdict was formed; re-run probe.py from a checkout at or after that commit"
        )
    if not on_origin(role):
        return None
    return (
        f"the k8s role {role!r} exists on origin/master but not in this checkout ({REPO}), "
        "which predates it — no workload was read; re-run probe.py from an up-to-date checkout"
    )
