"""Reject a commit message that holds the robot emoji (U+1F916).

The harness's attribution reminder ends every commit message with a footer that opens with
the robot emoji, and the operator's no-emoji rule wins over it. A squash merge makes the
message master history, so the prek `commit-msg` stage runs this on the message file git
passes it:

    uv run python scripts/dev/reject_robot_emoji.py .git/COMMIT_EDITMSG

Write the footer as `Generated with [Claude Code](https://claude.com/claude-code)`.

Exit codes: 0 the message is clean, 1 it holds the emoji, 64 bad arguments.
"""

import sys
from pathlib import Path

# `lib` is a sibling directory under `scripts/`; a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.cli_help import ArgumentParser, error

from lib.exit_codes import FAILED, OK

ROBOT = "\N{ROBOT FACE}"


def offending_lines(message: str) -> list[int]:
    """1-based numbers of the lines that hold the robot emoji, ignoring git's `#` comments."""
    return [
        number
        for number, line in enumerate(message.splitlines(), start=1)
        if ROBOT in line and not line.startswith("#")
    ]


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "message_file", type=Path, help="the commit message file git passes"
    )
    args = parser.parse_args(argv)
    lines = offending_lines(
        args.message_file.read_text(encoding="utf-8", errors="replace")
    )
    if not lines:
        return OK
    error(
        f"the commit message holds the robot emoji (U+1F916) on line(s) "
        f"{', '.join(map(str, lines))}; write the footer as "
        "`Generated with [Claude Code](https://claude.com/claude-code)`"
    )
    return FAILED


if __name__ == "__main__":
    sys.exit(main())
