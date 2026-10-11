"""Label this worktree with what it works on, for a worktree whose name Claude Code chose.

An `Agent` worktree is named `agent-<agentId>` and a Remote Control worktree `bridge-<sessionId>`,
and neither can be renamed. This writes a one-line `.claim` at the worktree root, which the
SessionStart banner and `prune_worktrees.py` print beside the name.

    uv run python scripts/dev/label_worktree.py 1345 1386     # "#1345 <title> +1"
    uv run python scripts/dev/label_worktree.py --text "Tune the hallway lights"
    uv run python scripts/dev/label_worktree.py --clear

Issue numbers are read from this repo's register with `gh`; `--text` reads nothing. It refuses
to write outside `.claude/worktrees/`, because a stray file in the primary checkout parks the
GitOps deployer.

Exit codes: 0 written or cleared, 1 the `gh` read failed, 64 bad arguments or not in a
worktree under `.claude/worktrees/`.
"""

import argparse
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

# `lib` is a sibling directory under `scripts/`; a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.cli_help import ArgumentParser

from lib.exit_codes import USAGE_ERROR

from lib.gh import gh_json
from lib.git import git_stdout
from lib.json_types import JsonValue, as_object
from lib.worktree_label import clear_label, write_label

WORKTREES_DIR = Path(".claude") / "worktrees"


def worktree_root(cwd: Path) -> Path | None:
    """The top level of the worktree `cwd` is in, when it sits under `.claude/worktrees/`."""
    try:
        top = git_stdout("rev-parse", "--show-toplevel", cwd=cwd)
    except OSError, subprocess.CalledProcessError:
        return None
    root = Path(top)
    return root if root.parent.parts[-2:] == WORKTREES_DIR.parts else None


def issue_label(numbers: list[int], read: Callable[..., JsonValue] = gh_json) -> str:
    """`#<first> <its title>`, with ` +<n>` for the issues after the first."""
    first = as_object(
        read("issue", "view", str(numbers[0]), "--json", "title"), "issue view"
    )
    label = f"#{numbers[0]} {first['title']}"
    others = ", ".join(f"#{n}" for n in numbers[1:])
    return f"{label} (+{others})" if others else label


def main(
    argv: list[str] | None = None,
    cwd: Path | None = None,
    read: Callable[..., JsonValue] = gh_json,
) -> int:
    parser = ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    what = parser.add_mutually_exclusive_group(required=True)
    what.add_argument("numbers", nargs="*", type=int, default=[], help="issue numbers")
    what.add_argument("--text", help="a free-text label instead of issue numbers")
    what.add_argument("--clear", action="store_true", help="remove the label")
    args = parser.parse_args(argv)

    root = worktree_root(cwd or Path.cwd())
    if root is None:
        sys.stderr.write(
            f"label_worktree: not inside a worktree under {WORKTREES_DIR}/; writing nothing\n"
        )
        return USAGE_ERROR
    if args.clear:
        print("cleared" if clear_label(root) else "no label to clear")
        return 0
    if args.text is not None:
        if not args.text.strip():
            sys.stderr.write("label_worktree: --text is empty\n")
            return USAGE_ERROR
        label = args.text
    else:
        try:
            label = issue_label(args.numbers, read)
        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            KeyError,
        ) as exc:
            sys.stderr.write(
                f"label_worktree: could not read #{args.numbers[0]}: {exc}\n"
            )
            return 1
    write_label(root, label)
    print(f"{root.name}: {label.strip()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
