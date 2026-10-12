"""SessionStart instructions-log rows for the files Claude Code loads without an event.

Claude Code fires no InstructionsLoaded event for the auto-memory `MEMORY.md` or for the
active output style, so `log-instructions.py` never rows them and `fact_status.py report`
under-counts each session's at-start bytes (#4656). session-health.py calls
`log_unevented_loads` once per session open to write those two rows itself.

Split out of session-health.py, which sits at its 600-line cap. Package name is `hooklib`,
not `lib`, for the reason `worktree_lines.py` gives.
"""

import json
import os
import re

from _hook_common import append_instructions_row, primary_checkout


def _claude_home():
    """The user-level Claude Code directory: `$CLAUDE_CONFIG_DIR`, else `~/.claude`."""
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(
        os.path.expanduser("~"), ".claude"
    )


def memory_md_path(cwd):
    """The auto-memory `MEMORY.md` a session started in `cwd` loads, or None.

    Claude Code keys the memory directory on the PRIMARY checkout, so a worktree session
    loads the primary's `MEMORY.md`, not one named after its own cwd. The directory name is
    the primary's real path with every non-alphanumeric character turned into `-`.
    """
    primary = primary_checkout(cwd)
    if not primary:
        return None
    slug = re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(primary))
    return os.path.join(_claude_home(), "projects", slug, "memory", "MEMORY.md")


def _frontmatter_name(path):
    """The `name:` field of the YAML frontmatter that opens `path`, or None."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            head = f.read(4096).splitlines()
    except OSError:
        return None
    if not head or head[0].strip() != "---":
        return None
    for line in head[1:]:
        if line.strip() == "---":
            return None
        key, sep, value = line.partition(":")
        if sep and key.strip() == "name":
            return value.strip().strip("\"'")
    return None


def output_style_path(cwd):
    """The file of the output style the settings name for a session in `cwd`, or None.

    The settings files are read in Claude Code's precedence order: the project's
    `settings.local.json`, then its `settings.json`, then the user's. A style is found by
    file name, then by frontmatter `name:`, in the project's `output-styles/` and then the
    user's. A built-in style has no file and so answers None.
    """
    home = _claude_home()
    project = os.path.join(cwd, ".claude")
    name = None
    for settings in (
        os.path.join(project, "settings.local.json"),
        os.path.join(project, "settings.json"),
        os.path.join(home, "settings.json"),
    ):
        try:
            with open(settings, encoding="utf-8") as f:
                value = json.load(f).get("outputStyle")
        except OSError, ValueError, AttributeError:
            continue
        if isinstance(value, str) and value:
            name = value
            break
    if name is None:
        return None
    dirs = [os.path.join(project, "output-styles"), os.path.join(home, "output-styles")]
    for d in dirs:
        candidate = os.path.join(d, name + ".md")
        if os.path.isfile(candidate):
            return candidate
    for d in dirs:
        try:
            entries = sorted(os.listdir(d))
        except OSError:
            continue
        for entry in entries:
            path = os.path.join(d, entry)
            if entry.endswith(".md") and _frontmatter_name(path) == name:
                return path
    return None


def log_unevented_loads(payload):
    """Row `MEMORY.md` and the output style in the instructions log, each with `bytes=`.

    `payload` is the SessionStart hook payload; its `session_id` keys the rows and its `cwd`
    picks the checkout. A payload with no session id writes nothing, and neither does a
    file that does not exist.
    """
    session = payload.get("session_id")
    if not session:
        return
    cwd = payload.get("cwd") or os.getcwd()
    for mtype, path in (
        ("AutoMem", memory_md_path(cwd)),
        ("Style", output_style_path(cwd)),
    ):
        if not path:
            continue
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        append_instructions_row(
            "session_start", mtype, path, session, f" bytes={size}", start=cwd
        )
