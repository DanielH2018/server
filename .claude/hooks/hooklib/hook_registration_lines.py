"""The SessionStart banner's arm for hook scripts this session registers but cannot run.

`.claude/settings.json` names every hook by an absolute path into the PRIMARY checkout, not
into the session's worktree. A worktree cut from a fresher `origin/master` than the primary
checkout therefore registers hook scripts the primary checkout does not have: `/bin/sh` exits
127, Claude Code logs a non-blocking hook error, and the matching tool call runs with the guard
skipped. About 2,100 Bash calls ran that way on daniel-server across two windows in September
2026.

`fanout_lib/launch.py:fast_forward_primary_command` closed the fan-out half by fast-forwarding
the host's primary checkout before it creates the worktree. This arm covers the hand-made half —
a worktree `EnterWorktree` created — where nothing fast-forwards anything.

Two limits, stated rather than implied:

  * **This cannot report its own absence.** `run-hook.sh` and `session-health.py` are files a
    behind primary checkout may lack, and a SessionStart hook that does not exist prints nothing. The
    window this arm closes is "the primary checkout is missing SOME hook scripts"; the window
    where it is missing THIS one stays open, and only a fast-forward closes it.
  * **It rules on the `.py` sibling only in the two forms this repo's shims use.** A per-hook
    shim runs its sibling as `"$(dirname "$(readlink -f "$0")")/<name>.py"`, and `run-hook.sh`
    composes the same path from its first argument. Both are matched as text rather than parsed
    as shell. A shim naming its `.py` some other way gets no verdict — abstaining is the posture
    `script_path` already takes for a path it cannot resolve, and
    `test_the_repos_own_shims_name_the_siblings_this_parse_must_find` is what keeps abstention
    from quietly becoming the whole answer.

Split out of session-health.py, which sits at its own 600-line cap (`ansible/tests/_ratchet.py`)
with no headroom left. Package name is `hooklib`, not `lib`, for the reason session-health.py's
own import comment gives.
"""

import json
import os
import re
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
    `cwd` to the first directory holding a `.claude/settings.json`, which is the worktree root
    for a worktree session and the primary checkout for a session in it. `cwd` is the
    SessionStart payload's own field where the caller has it — `block-protected-bash.py` and
    `inject-nested-docs.py` read the same field — and this process's cwd otherwise.

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


# The `.py` sibling a shim runs from its own location:
#
#     exec /home/ubuntu/.local/bin/uv run --no-sync --quiet python \
#       "$(dirname "$(readlink -f "$0")")/bash-pretool.py"
#
# One literal idiom, matched as text. Reading a shim in general means parsing shell; five of
# this repo's eight shims share this one form, so matching the form covers every sibling that
# exists without a parser. Anything else abstains, because a banner line that cries wolf over
# a working hook is worse than one that stays quiet about an odd one — `script_path`'s own
# docstring makes the same trade.
_SIBLING_PY = re.compile(
    r"""\$\(\s*dirname\s+"?\$\(\s*readlink\s+-f\s+"\$0"\s*\)"?\s*\)/([A-Za-z0-9_.-]+\.py)"""
)

# The same directory, with the hook's name coming from a variable instead of the text:
#
#     HOOKS_DIR="$(dirname "$(readlink -f "$0")")"
#     script="$HOOKS_DIR/$name.py"
#
# which is how `run-hook.sh` runs the hook named by its first argument (#3278). The name is not
# in the shim at all, so it comes from the registered COMMAND. Matching the composing line
# rather than the runner's filename keeps the two halves of this rule in one place: a runner
# that stops composing its sibling that way resolves nothing and fails
# `test_the_repos_own_shims_name_the_siblings_this_parse_must_find` by name.
_RUNNER_SIBLING_PY = re.compile(r"""\$\{?(?:\w+)\}?/\$\{?\w+\}?\.py""")


def _runner_hook_name(command):
    """The hook name a `run-hook.sh <name> [--flags]` command passes, or None.

    The first word is the script itself; the first argument after it that is not a flag is the
    name. Anything `shlex` cannot read, or a command passing no name, gets None — the same
    abstention `script_path` takes for a path it cannot resolve.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    for token in tokens[1:]:
        if not token.startswith("-"):
            return token
    return None


def sibling_py_paths(shim_path, command=None, read_text=None):
    """The `.py` files the shim at `shim_path` runs out of its own directory, in file order.

    Args:
        shim_path: the resolved `.sh` the registration names.
        command: the registered command, for a runner that takes the hook name as an argument.
            Optional, because the literal form needs only the shim's own text.
        read_text: returns the shim's text. Defaults to reading the file.

    `$(dirname "$(readlink -f "$0")")` is the directory of the RESOLVED shim — the primary
    checkout's `.claude/hooks/`, since `settings.json` names the shim by an absolute path there.
    So the sibling is joined to `shim_path`'s directory and never to the session's checkout:
    joining it to the checkout would point the whole arm at the worktree, which has the file.

    An unreadable shim yields [], the same best-effort posture as an unparsable settings file.
    """
    if not shim_path.endswith(".sh"):
        return []
    if read_text is None:

        def read_text(path):
            with open(path, encoding="utf-8") as handle:
                return handle.read()

    try:
        text = read_text(shim_path)
    except OSError:
        return []
    directory = os.path.dirname(shim_path)
    names = []
    for name in _SIBLING_PY.findall(text):
        if name not in names:
            names.append(name)
    if not names and command is not None and _RUNNER_SIBLING_PY.search(text):
        hook_name = _runner_hook_name(command)
        if hook_name:
            names.append(f"{hook_name}.py")
    return [os.path.join(directory, name) for name in names]


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


def _shown(paths):
    """The paths a line names, with a count standing in for the rest past `MISSING_LIMIT`."""
    shown = ", ".join(paths[:MISSING_LIMIT])
    if len(paths) > MISSING_LIMIT:
        shown += f", +{len(paths) - MISSING_LIMIT} more"
    return shown


def _with_fix(line, paths):
    """`line` plus the fast-forward for `paths`, where the first path names its checkout."""
    fix = _fix_command(paths[0])
    if not fix:
        return line
    return line + f"; fast-forward the checkout that holds them: `{fix}`"


def missing_hook_script_lines(
    checkout=None, read_settings=None, exists=None, cwd=None, read_text=None
):
    """Banner lines for the hook scripts this session registers and cannot run, or [].

    Up to two lines, because the two failure modes have different symptoms and an operator
    reading one diagnosis must not be handed the other's:

      * a registered `.sh` the primary checkout lacks — `/bin/sh` exits 127 and Claude Code
        logs a non-blocking hook error;
      * a `.py` sibling that shim runs, which the primary checkout lacks — the shim RUNS, so
        nothing exits 127 and the guard is skipped anyway.

    Args:
        checkout: the session's checkout. Defaults to `session_checkout(cwd=cwd)`.
        cwd: the session's directory, for the walk `session_checkout` falls back to. The
            SessionStart payload's `cwd` where the caller has it, since the hook's own process
            cwd is whatever Claude Code launched it with.
        read_settings: returns the parsed `.claude/settings.json` of `checkout`. Defaults to
            reading and parsing it.
        exists: path predicate. Defaults to `os.path.exists`.
        read_text: returns a shim's text, for `sibling_py_paths`. Defaults to reading the file.

    Seams are parameters rather than patched attributes, like `parked_deployer_problems`: the
    monkeypatch ratchet (`ansible/tests/_ratchet.py`) caps a new test module at zero patches on
    a first-party module.

    Best-effort like every other check in this banner: an unreadable or unparsable settings file
    returns [], because a SessionStart hook must never block a session from starting, and a
    settings file Claude Code itself could not parse registered no hooks to be missing.
    """
    checkout = session_checkout(cwd=cwd) if checkout is None else checkout
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
    missing_shims = []
    missing_siblings = []
    for command in registered_hook_commands(settings):
        path = script_path(command, checkout)
        if not path:
            continue
        # A missing shim is not also read for siblings: the shim that would name them is the
        # file that is gone, so every sibling it names is a guess, and the fast-forward the
        # first line already carries is the same one either way.
        if not exists(path):
            if path not in missing_shims:
                missing_shims.append(path)
            continue
        for sibling in sibling_py_paths(path, command=command, read_text=read_text):
            if not exists(sibling) and sibling not in missing_siblings:
                missing_siblings.append(sibling)
    lines = []
    if missing_shims:
        lines.append(
            _with_fix(
                f"  ✗ {len(missing_shims)} hook script(s) this session registers do not "
                f"exist: {_shown(missing_shims)} — /bin/sh exits 127, Claude Code logs a "
                "non-blocking hook error, and every matching tool call runs with that guard "
                "SKIPPED",
                missing_shims,
            )
        )
    if missing_siblings:
        lines.append(
            _with_fix(
                f"  ✗ {len(missing_siblings)} .py script(s) a registered shim runs do not "
                f"exist: {_shown(missing_siblings)} — the shim is there, so it RUNS: "
                "`uv run` cannot find the script, and the guard is SKIPPED behind a hook that "
                "reports no error",
                missing_siblings,
            )
        )
    return lines
