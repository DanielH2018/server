"""The SessionStart banner's arm for hook scripts this session registers but cannot run.

`.claude/settings.json` names every hook by an absolute path into the PRIMARY checkout, not
into the session's worktree. A worktree cut from a fresher `origin/master` than the primary
checkout therefore registers hook scripts the primary checkout does not have: `/bin/sh` exits
127, Claude Code logs a non-blocking hook error, and the matching tool call runs with the guard
skipped. About 2,100 Bash calls ran that way on daniel-server across two windows in September
2026 (issue #2675).

`fanout_lib/launch.py:fast_forward_primary_command` closed the fan-out half by fast-forwarding
the host's primary checkout before it creates the worktree. This arm covers the hand-made half —
a worktree `EnterWorktree` created — where nothing fast-forwards anything (issue #2697).

Two limits, stated rather than implied:

  * **This cannot report its own absence.** `session-health.sh` is itself a file a behind
    primary checkout may lack, and a SessionStart hook that does not exist prints nothing. The
    window this arm closes is "the primary checkout is missing SOME hook scripts"; the window
    where it is missing THIS one stays open, and only a fast-forward closes it.
  * **It sees the `.sh` shims only, because that is all `settings.json` names.** Every shim runs
    a `.py` sibling it resolves itself, so a present `session-health.sh` beside a missing
    `session-health.py` reads as covered here. That failure surfaces as a hook that runs and
    does nothing instead of one that exits 127.

Split out of session-health.py, which sits at its own 600-line cap (`ansible/tests/_ratchet.py`)
with no headroom left. Package name is `hooklib`, not `lib`, for the reason session-health.py's
own import comment gives.
"""

import json
import os
import shlex

# How many missing scripts the line names before it counts the rest. A behind checkout is
# usually missing one or two; a wholesale mismatch does not need every name to be actionable.
MISSING_LIMIT = 6


def session_checkout(env=None, cwd=None):
    """The checkout whose `.claude/settings.json` Claude Code read for THIS session, or None.

    Reading the PRIMARY checkout's own `settings.json` instead would make the whole arm
    self-agreeing: it would compare a file against the `.claude/hooks/` directory it was
    committed beside and never disagree. So this resolves the SESSION's checkout and never
    falls back to `session-health.py`'s `REPO`, which is the primary checkout by construction
    (Claude Code invokes the hook by its absolute path there).

    `$CLAUDE_PROJECT_DIR` is the direct answer and is preferred. No hook in this repo read it
    before, so its presence here is unverified rather than assumed: the fallback walks up from
    the session's cwd to the first directory holding a `.claude/settings.json`, which is the
    worktree root for a worktree session and the primary checkout for a session in it.

    A session in the primary checkout resolves to the primary checkout, where the comparison is
    the self-agreeing one above and prints nothing. That is the correct answer for it — there is
    no second checkout in play — not a gap this can close.
    """
    env = os.environ if env is None else env
    project_dir = env.get("CLAUDE_PROJECT_DIR")
    if project_dir:
        return project_dir
    path = os.path.abspath(os.getcwd() if cwd is None else cwd)
    while True:
        if os.path.isfile(os.path.join(path, ".claude", "settings.json")):
            return path
        parent = os.path.dirname(path)
        if parent == path:
            return None
        path = parent


def registered_hook_commands(settings):
    """Every `command` string under `hooks.<event>[].hooks[]`, in file order.

    Defensive at every level rather than at the top: a settings file whose shape this does not
    recognise yields the commands it does recognise, not nothing. An empty list here reads as
    "no hook is missing", which is what a healthy checkout reads as too — so a walk that
    silently stopped matching would look exactly like health. The non-vacuity test over this
    repo's own `settings.json` is what refuses that.
    """
    commands = []
    events = settings.get("hooks") if isinstance(settings, dict) else None
    if not isinstance(events, dict):
        return commands
    for entries in events.values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            hooks = entry.get("hooks") if isinstance(entry, dict) else None
            if not isinstance(hooks, list):
                continue
            for hook in hooks:
                command = hook.get("command") if isinstance(hook, dict) else None
                if isinstance(command, str) and command.strip():
                    commands.append(command)
    return commands


def script_path(command, checkout):
    """The file `/bin/sh` execs for `command`, or None when that is not decidable here.

    None for anything whose existence this arm cannot rule on: a bare name resolved through
    `PATH`, a command `shlex` cannot parse, or a path still carrying an unexpanded `$VAR` after
    expansion. Each of those would otherwise read as missing, and a banner that cries wolf over
    a working hook is worse than one that stays quiet about an odd one.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    if not tokens:
        return None
    first = tokens[0]
    if "/" not in first:
        return None
    # Substituted before `expandvars` because the variable Claude Code documents for a hook
    # command is often absent from this process's own environment, and `expandvars` would then
    # leave it in place for the `"$" in path` guard below to discard.
    for form in ("${CLAUDE_PROJECT_DIR}", "$CLAUDE_PROJECT_DIR"):
        first = first.replace(form, checkout)
    path = os.path.expanduser(os.path.expandvars(first))
    if "$" in path:
        return None
    if not os.path.isabs(path):
        path = os.path.join(checkout, path)
    return os.path.normpath(path)


def _fix_command(path):
    """The fast-forward that restores `path`, when its checkout root is derivable from it.

    A script at `<root>/.claude/hooks/<name>` names its own checkout. Anything else gets no
    command rather than a guessed one — the paths are in the line either way.
    """
    hooks_dir, _name = os.path.split(path)
    claude_dir, hooks = os.path.split(hooks_dir)
    root, claude = os.path.split(claude_dir)
    if hooks != "hooks" or claude != ".claude" or not root:
        return ""
    return f"git -C {root} merge --ff-only origin/master"


def missing_hook_script_lines(checkout=None, read_settings=None, exists=None):
    """One banner line for the hook scripts this session registers and cannot run, or [].

    Args:
        checkout: the session's checkout. Defaults to `session_checkout()`.
        read_settings: returns the parsed `.claude/settings.json` of `checkout`. Defaults to
            reading and parsing it.
        exists: path predicate. Defaults to `os.path.exists`.

    Seams are parameters rather than patched attributes, like `parked_deployer_problems`: the
    monkeypatch ratchet (`ansible/tests/_ratchet.py`) caps a new test module at zero patches on
    a first-party module.

    Best-effort like every other check in this banner: an unreadable or unparsable settings file
    returns [], because a SessionStart hook must never block a session from starting, and a
    settings file Claude Code itself could not parse registered no hooks to be missing.
    """
    checkout = session_checkout() if checkout is None else checkout
    if not checkout:
        return []
    if read_settings is None:

        def read_settings():
            with open(
                os.path.join(checkout, ".claude", "settings.json"), encoding="utf-8"
            ) as handle:
                return json.load(handle)

    exists = os.path.exists if exists is None else exists
    try:
        settings = read_settings()
    except OSError, ValueError:
        return []
    missing = []
    for command in registered_hook_commands(settings):
        path = script_path(command, checkout)
        if path and not exists(path) and path not in missing:
            missing.append(path)
    if not missing:
        return []
    shown = ", ".join(missing[:MISSING_LIMIT])
    if len(missing) > MISSING_LIMIT:
        shown += f", +{len(missing) - MISSING_LIMIT} more"
    fix = _fix_command(missing[0])
    line = (
        f"  ✗ {len(missing)} hook script(s) this session registers do not exist: {shown} — "
        "/bin/sh exits 127, Claude Code logs a non-blocking hook error, and every matching "
        "tool call runs with that guard SKIPPED"
    )
    if fix:
        line += f"; fast-forward the checkout that holds them: `{fix}`"
    return [line]
