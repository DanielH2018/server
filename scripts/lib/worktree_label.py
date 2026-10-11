"""The `.claim` label a session writes into a worktree whose name Claude Code generated.

Two paths create worktrees with names nobody chose: an `Agent` call with `isolation:
"worktree"` gets `agent-<agentId>`, and a Remote Control session gets `bridge-<sessionId>`.
Neither can be renamed at creation (`docs/issue-claiming-and-fanout.md`, *Measured: a
subagent cannot own a named worktree*), so the session inside writes one line saying what it
works on, and every report that lists worktrees prints that line beside the name.

The file sits at the worktree root, where the root deny in `.gitignore` keeps it out of every
commit. It dies with the worktree, so nothing has to clean it up.

Typical usage example:

    write_label(tree, "#1345 Fix the cert watcher")
    read_label(tree)  # "#1345 Fix the cert watcher"
"""

from pathlib import Path

LABEL_FILE = ".claim"

# A banner line names several sessions; a label longer than this pushes the changed-paths
# list off the line it shares.
MAX_LABEL_CHARS = 80

# The prefixes Claude Code gives a worktree it named itself.
GENERATED_PREFIXES = ("agent-", "bridge-")


def has_generated_name(tree: str | Path) -> bool:
    """Whether Claude Code, not a session, chose this worktree's directory name."""
    return Path(tree).name.startswith(GENERATED_PREFIXES)


def read_label(tree: str | Path) -> str | None:
    """The first line of the tree's `.claim`, cut to `MAX_LABEL_CHARS`, or None.

    An unreadable or empty file reads as None: a report must still print the tree.
    """
    try:
        text = (Path(tree) / LABEL_FILE).read_text(encoding="utf-8")
    except OSError, UnicodeDecodeError:
        return None
    first = text.strip().splitlines()[0].strip() if text.strip() else ""
    if not first:
        return None
    if len(first) > MAX_LABEL_CHARS:
        first = first[: MAX_LABEL_CHARS - 1] + "…"
    return first


def write_label(tree: str | Path, label: str) -> Path:
    """Writes `label` as the tree's `.claim` and returns the file's path."""
    path = Path(tree) / LABEL_FILE
    path.write_text(label.strip() + "\n", encoding="utf-8")
    return path


def clear_label(tree: str | Path) -> bool:
    """Removes the tree's `.claim`. Returns whether there was one."""
    try:
        (Path(tree) / LABEL_FILE).unlink()
    except FileNotFoundError:
        return False
    return True


def with_label(name: str, tree: str | Path) -> str:
    """`name`, followed by ` [<label>]` when the tree carries one."""
    label = read_label(tree)
    return f"{name} [{label}]" if label else name
