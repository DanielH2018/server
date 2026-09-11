#!/usr/bin/env python3
"""Operate on the GitOps deployer's own state markers, from the deploy host's shell.

One subcommand today. `clear-manual-plane <role>` drops a role's line from
`/var/lib/gitops-deploy/manual_plane`, the marker the deployer writes when a range carries a
setup role no playbook it runs can apply — `k3s` (applied by `k3s-bringup.yml`) or `common`
(applied by no playbook at all). The tick fast-forwards past such a range rather than parking
it, so the marker is what says the apply is still owed: monitor-bridge pages once the oldest
pending role is six hours old, and `land.sh` prints the same clear command.

**The apply comes first, this second.** Clearing a role nobody applied silences the only
durable signal that it is unapplied, which is the state the marker exists to make visible.

This is not a path the deployer takes. Its own reverse is
`DeployerState.clear_manual_plane_applied`, which fires when a tick applies the role's real
playbook and tag — unreachable today, since it runs neither of the two playbooks in question.

WHERE IT RUNS. `/var/lib/gitops-deploy` is 0750 and owned by `sys_user` (`ubuntu` on
daniel-box), so the deploy user's own shell writes it directly and any other user needs
`sudo -u ubuntu`. A directory this uid cannot write is reported as that, not as a traceback.

Run: uv run pytest scripts/deploy_tools/tests/test_gitops_state.py
"""

import argparse
import sys

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
from deploy_state import STATE_DIR, DeployerState


def marker_key(role: str) -> str:
    """The `manual_plane` line key for a role, which is the `--tags` value that selects it."""
    return setup_role_tag(role)


def clear_manual_plane(state: DeployerState, role: str) -> int:
    """Drop `role`'s pending line. Exit 0 whether or not there was one to drop."""
    key = marker_key(role)
    try:
        cleared = state.clear_manual_plane(key)
    except PermissionError:
        print(
            f"cannot write {state.path('manual_plane')} as this user — the state directory "
            "is owned by the deploy user; retry with `sudo -u ubuntu`",
            file=sys.stderr,
        )
        return 1
    if not cleared:
        print(
            f"{role} is not pending in {state.path('manual_plane')} — nothing to clear"
        )
        return 0
    print(f"cleared {role} from {state.path('manual_plane')}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
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
    args = parser.parse_args(argv)
    state = DeployerState(args.state_dir)
    return clear_manual_plane(state, args.role)


if __name__ == "__main__":
    sys.exit(main())
