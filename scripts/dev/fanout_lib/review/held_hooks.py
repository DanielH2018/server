"""The project hooks and settings a review pipeline holds from start, outside the worktree.

WHY. Every phase of a server fan-out batch runs in a worktree the implementer can write, and
this repo's `.claude/settings.json` registers every project hook from that tree. A phase that
loaded both after the implementer ran would run whatever it left there (#3794, #3810). The
pipeline reads the files once at start with `read_hooks`, rewrites a copy outside the worktree
before every phase with `write_hooks`, and hands a resumed phase the start-time settings with
every hook command pointed at that copy (`pointed_settings`).

DECIDED: in this repo's batch, every phase that starts after the implementer ran (review, fix,
review-delta, land, file) runs with `--setting-sources user`. Another repo's batch keeps its
project settings: the pipeline holds no copy of that repo's hooks, so dropping the source would
drop its guards. A probe on 2026-10-09 showed that the flag also drops the project `CLAUDE.md`
and the project skills from a fresh session. A resumed session already holds `CLAUDE.md` in its
transcript, except a red batch's, which gets the start-time copy again (below). The land text
carries the whole `land.sh` command. The reviewer is a fresh session, so the pipeline appends
the `CLAUDE.md` it read at start to the review prompt (#3825). The reviewer loses the project skills and `.claude/rules/`; it reads files rather
than following a procedure, and its prompt names the role `CLAUDE.md` files to check.

DECIDED: the implement phase keeps the project source only when no red phase ran before it.
Without one it starts from the tree `worktree add` checked out, which no agent has written,
and a fresh implementer needs the project skills and `.claude/rules/`. After a red phase the
red author has had the worktree, and the red gate reads only the tracked range `base..red`. An
ignored `.claude/settings.local.json`, a hook edit hidden with `--skip-worktree` or a planted
`.pyc` is outside that range (#3846). That implement phase therefore runs on the held settings
and gets the start-time `CLAUDE.md` as text, and so does every phase that resumes it. Like the reviewer, it loses the project skills and
rules.

`block-protected-bash` derives its secret-bearing host paths from the session's cwd, which is
the worktree. The pipeline derives the set at start and ships it beside the held hooks as
`HELD_SECRET_PATHS`, which that guard reads in place of the derivation.
"""

import json
import shlex
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

HOOKS_DIR = Path(".claude") / "hooks"
# Directories under `HOOKS_DIR` no hook runs from. A stale `__pycache__` copied into the
# snapshot would also outlive the source it was compiled from.
UNSNAPSHOTTED = frozenset({"tests", "__pycache__"})
# How this repo's `.claude/settings.json` names its hooks directory in every hook command.
PROJECT_HOOKS = '"$CLAUDE_PROJECT_DIR"/.claude/hooks/'
# The derivation `block-protected-bash` runs, and the file beside the held hooks that it reads
# in its place. `.claude/hooks/block-protected-bash.py` mirrors the file name.
SECRET_PATHS_SCRIPT = "scripts/secrets_mgmt/secret_bearing_host_paths.py"
HELD_SECRET_PATHS = "secret_bearing_host_paths.json"

Runner = Callable[[list[str], str | None], subprocess.CompletedProcess]


def read_hooks(root: Path) -> dict[str, bytes]:
    """Every file a hook under `root` can run or import, by path relative to `HOOKS_DIR`."""
    hooks = root / HOOKS_DIR
    return {
        path.relative_to(hooks).as_posix(): path.read_bytes()
        for path in sorted(hooks.rglob("*"))
        if path.is_file()
        and not UNSNAPSHOTTED & set(path.relative_to(hooks).parts[:-1])
    }


def write_hooks(hooks: dict[str, bytes], root: Path) -> Path:
    """Replace `root`'s hooks directory with `hooks` and return its `fanout-stop.py`.

    Every process here runs as one user, so no directory is out of the agent's reach.
    Rewriting the bytes before each phase is what makes the copy the pipeline's: an edit the
    agent makes to it lasts until the next phase starts, never into it. The directory is
    removed first because it is each hook's `sys.path[0]`, so a planted `json.py` would
    shadow the stdlib module for every hook that imports it.
    """
    target = root / HOOKS_DIR
    if target.exists():
        shutil.rmtree(target)
    for name, data in hooks.items():
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(0o555 if name.endswith(".sh") else 0o444)
    return target / "fanout-stop.py"


def pointed_settings(settings: str, root: Path) -> dict:
    """The project settings text `settings`, with every hook command run from `root`'s copy.

    The set already registers `fanout-stop`, so a phase given it needs no separate Stop hook.
    """
    held = json.loads(settings)
    hooks = shlex.quote(str(root / HOOKS_DIR)) + "/"
    for groups in held.get("hooks", {}).values():
        for group in groups:
            for hook in group.get("hooks", []):
                hook["command"] = hook["command"].replace(PROJECT_HOOKS, hooks)
    return held


def held_secret_paths(run: Runner, root: Path) -> dict[str, bytes]:
    """The `HELD_SECRET_PATHS` file to ship beside the held hooks, derived from `root` now.

    Returns:
        The file name and its JSON bytes, or nothing when the derivation failed. The guard
        then falls back to deriving the set itself.
    """
    proc = run(
        ["uv", "run", "--directory", str(root), "python", SECRET_PATHS_SCRIPT], None
    )
    if proc.returncode:
        return {}
    paths = {}
    for line in proc.stdout.splitlines():
        dest, sep, names = line.partition("\t")
        if sep:
            paths[dest] = names.split(",")
    return {HELD_SECRET_PATHS: json.dumps(paths).encode()}
