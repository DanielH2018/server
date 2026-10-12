"""block-protected-bash's third arm: a read-only agent may not write through Bash (#4617).

Split out of `block-protected-bash.py`, which sat near its 600-line cap
(`ansible/tests/_ratchet.py`). `block-protected-bash.py`'s `decision` calls
`read_only_decision` before its other two arms and hands it that file's `written_paths`, so
the redirect detector stays one function shared by three callers.
"""

import os
import re

from _hook_common import (
    Unsplittable,
    command_substitutions,
    git_invocation,
    split_stages,
    strip_shell_keywords,
)
from hooklib import read_only_commands as commands

_OWN_CHECKOUT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

# An agent whose frontmatter carries `read_only: true` still lists Bash, and Bash can write a
# file, commit and push. The description's "Read-only" was a prompt instruction, and these
# agents read untrusted text: the skeptic reads open PRs, and a Renovate PR body quotes
# upstream release notes (#4617). A subagent's hook payload names it in `agent_type`, its
# frontmatter `name:`.
#
# DECIDED: deny, not arm 1's ask. Arm 1 asks because one wrong extraction must not block the
# operator's own session. Here the session is an agent that has declared it writes nothing, so
# a false positive costs it one rephrased read, while a missed write is the injected action the
# arm exists to stop. Outside git and gh, each command is judged against the allowlist in
# `read_only_commands.py`, whose docstring says why it is an allowlist. A wrapper (`timeout`,
# `env`, `xargs`, `uv run`, …) is read past, an `ssh` remote command and a `$( )` body are
# judged as commands of their own, and a nested shell or `eval` is denied.

_AGENTS_DIR = os.path.join(_OWN_CHECKOUT, ".claude", "agents")


def read_only_agents(agents_dir=_AGENTS_DIR):
    """The `name:` of every agent whose frontmatter sets `read_only: true`.

    Read from the checkout this file sits in, as every bash-pretool arm's code is. A directory
    that cannot be read gives the empty set: the arm then judges nothing, the posture every
    guard here takes when it cannot compute its answer.
    """
    names = set()
    try:
        entries = sorted(os.listdir(agents_dir))
    except OSError:
        return names
    for entry in entries:
        if not entry.endswith(".md"):
            continue
        try:
            with open(os.path.join(agents_dir, entry)) as agent:
                text = agent.read()
        except OSError:
            continue
        if not text.startswith("---"):
            continue
        fields = {}
        for line in text[3:].split("\n---", 1)[0].splitlines():
            key, sep, value = line.partition(":")
            if sep and not key.startswith((" ", "\t")):
                fields[key.strip()] = value.strip()
        if fields.get("read_only", "").lower() == "true" and fields.get("name"):
            names.add(fields["name"])
    return names


# A write that changes nothing the repo, the cluster or GitHub holds. `2>/dev/null` is in nearly
# every reviewer command, and a scratch file under /tmp is how an agent stages a payload to read.
_SCRATCH_PREFIXES = (
    "/dev/",
    "/tmp/",
    "/var/tmp/",
    os.path.expanduser("~/.cache/claude/"),
)


def _is_scratch(path, repo_root):
    """True when `path` resolves under a scratch directory.

    A relative path is never scratch: the hook resolves it against the session's cwd, and a
    `cd` earlier in the same command moves where the shell resolves it. `realpath`, not
    `normpath`: `> /tmp/l` writes the repo when `/tmp/l` already links into it. A link the
    same command creates is refused at the `ln`, which is not a read.
    """
    expanded = os.path.expanduser(path)
    if not os.path.isabs(expanded):
        return False
    target = os.path.realpath(expanded)
    return (target + "/").startswith(_SCRATCH_PREFIXES)


# git subcommands that write nothing whatever their arguments. `fetch` is not one: a refspec
# such as `+x:refs/heads/master` rewrites a local branch in the `.git` every worktree shares.
_GIT_READS = {
    "status",
    "log",
    "diff",
    "show",
    "describe",
    "rev-parse",
    "rev-list",
    "ls-files",
    "ls-tree",
    "ls-remote",
    "blame",
    "shortlog",
    "whatchanged",
    "cat-file",
    "for-each-ref",
    "show-ref",
    "grep",
    "name-rev",
    "merge-base",
    "check-ignore",
    "check-attr",
    "count-objects",
    "cherry",
    "range-diff",
    "var",
    "help",
    "version",
}
# The flags that turn `git branch` / `git tag` from a listing into a change.
_REF_MUTATING_LONG = {
    "--delete",
    "--move",
    "--copy",
    "--force",
    "--set-upstream-to",
    "--unset-upstream",
    "--edit-description",
    "--track",
    "--annotate",
    "--sign",
    "--message",
    "--file",
    "--edit",
}
# The short flags differ: `-a` lists every branch but annotates a tag.
_BRANCH_MUTATING_SHORT = set("dDmMcCfut")
_TAG_MUTATING_SHORT = set("dfasumFe")
# The flags under which a positional argument is a pattern or a commit, not a ref to create.
_REF_LIST_MODE = {
    "-l",
    "--list",
    "--contains",
    "--no-contains",
    "--merged",
    "--no-merged",
    "--points-at",
}


def _ref_listing(args, mutating_short):
    """True when `git branch` / `git tag` with `args` only lists refs."""
    if any(_ref_flag_mutates(a, mutating_short) for a in args):
        return False
    # A positional names a ref to create, unless a list-mode flag makes it a pattern or commit.
    if any(a.split("=", 1)[0] in _REF_LIST_MODE for a in args):
        return True
    return all(a.startswith("-") for a in args)


def _ref_flag_mutates(arg, mutating_short):
    if arg.startswith("--"):
        return arg.split("=", 1)[0] in _REF_MUTATING_LONG
    return arg.startswith("-") and bool(mutating_short & set(arg[1:]))


def _first(args):
    return args[0] if args else ""


# Subcommands with a read form and a write form, and the test for the read form.
_GIT_READ_FORMS = {
    "branch": lambda a: _ref_listing(a, _BRANCH_MUTATING_SHORT),
    "tag": lambda a: _ref_listing(a, _TAG_MUTATING_SHORT),
    "remote": lambda a: _first(a) in ("", "-v", "--verbose", "show", "get-url"),
    "worktree": lambda a: _first(a) == "list",
    "stash": lambda a: _first(a) in ("list", "show"),
    "reflog": lambda a: _first(a) in ("", "show") or _first(a).startswith("-"),
    "config": lambda a: (
        bool({"--get", "--get-all", "--get-regexp", "--list", "-l"} & set(a))
        and not {"--unset", "--unset-all", "--add", "--replace-all", "--edit", "-e"}
        & set(a)
    ),
}


def _git_problem(stage):
    """Why this git stage writes, or None when it is a read."""
    inv = git_invocation(stage)
    if inv is None:
        return None
    if inv.shell or not inv.sub:
        return "runs a git alias whose command this hook cannot see"
    if inv.config:
        # `-c core.pager=<cmd>` runs a command under any subcommand, `git log` included.
        return "sets git config inline"
    if any(o.split("=", 1)[0] == "--exec-path" for o in inv.global_opts):
        return "points git at another directory of subcommands"
    args = list(inv.args)
    # `--upload-pack=<cmd>` and its kin run a command under `ls-remote`, which is otherwise a read.
    if any(
        a.split("=", 1)[0] in ("--upload-pack", "--receive-pack", "--exec")
        or (inv.sub == "ls-remote" and a == "-u")
        for a in args
    ):
        return "names a command for git to run"
    # `--output=<file>` writes the diff under every subcommand that takes diff options, and
    # `git grep -O<cmd>` runs a command on the matches.
    if any(a.split("=", 1)[0] == "--output" for a in args):
        return "writes git output to a file"
    if inv.sub == "grep" and any(
        re.match(r"-[A-Za-z]*O", a) or a.startswith("--open-files-in-pager")
        for a in args
    ):
        return "runs a pager command from git grep"
    if inv.sub in _GIT_READS:
        return None
    read_form = _GIT_READ_FORMS.get(inv.sub)
    if read_form and read_form(args):
        return None
    return f"runs `git {inv.sub}`, which is not in the read-only set"


# `gh <group> <verb>` verbs that only read. Every other verb comments, merges, edits, creates,
# closes, reruns or downloads.
_GH_READ_VERBS = {"view", "list", "ls", "status", "diff", "checks", "watch"}
_GH_READ_GROUPS = {"search", "status", "help", "version", "--version"}
_GH_VALUE_FLAGS = {"-R", "--repo", "--hostname"}
# `gh api` sends a POST as soon as a field is given, whatever the endpoint.
_GH_API_BODY = ("-f", "-F", "--field", "--raw-field", "--input")


def _gh_api_problem(args):
    method = "GET"
    for i, arg in enumerate(args):
        if arg in ("-X", "--method"):
            method = args[i + 1] if i + 1 < len(args) else ""
        elif arg.startswith("--method="):
            method = arg.split("=", 1)[1]
        elif arg.startswith("-X") and len(arg) > 2:
            method = arg[2:]
        elif arg.split("=", 1)[0] in _GH_API_BODY or (
            arg[:2] in ("-f", "-F") and len(arg) > 2
        ):
            return "sends a request body through `gh api`, which makes it a POST"
    if method.upper() != "GET":
        return f"sends a {method or 'missing'} request through `gh api`"
    return None


def _gh_problem(stage):
    """Why this gh stage writes, or None when it is a read."""
    words, i = stage[1:], 0
    positional = []
    while i < len(words):
        word = words[i]
        if word in _GH_VALUE_FLAGS:
            i += 2
            continue
        if word.startswith("-") and not positional and word != "--version":
            i += 1
            continue
        positional.append(word)
        if len(positional) == 2 or (len(positional) == 1 and word == "api"):
            break
        i += 1
    group = _first(positional)
    if group == "api":
        return _gh_api_problem(words[i + 1 :])
    if group in _GH_READ_GROUPS:
        return None
    verb = positional[1] if len(positional) > 1 else ""
    if verb in _GH_READ_VERBS:
        return None
    return f"runs `{' '.join(['gh', *positional])}`, which is not in the read-only set"


_UV_RUN_VALUE_FLAGS = frozenset(
    {
        "--with",
        "--with-requirements",
        "--with-editable",
        "--project",
        "--directory",
        "--python",
        "-p",
        "--group",
        "--only-group",
        "--no-group",
        "--extra",
        "--env-file",
        "--package",
        "--index",
    }
)
_XARGS_VALUE_FLAGS = frozenset(
    {"-I", "-n", "-P", "-L", "-d", "-E", "-s", "-a", "--arg-file", "--delimiter"}
)
_TIMEOUT_VALUE_FLAGS = frozenset({"-s", "-k", "--signal", "--kill-after"})
_SSH_VALUE_FLAGS = frozenset(
    "-b -c -D -E -e -F -I -i -J -L -l -m -O -o -p -Q -R -S -W -w -B".split()
)
_SYSTEM_BIN = ("/usr/bin/", "/bin/", "/usr/sbin/", "/sbin/")


def _skip_flags(words, i, value_flags=frozenset()):
    """The index of the first word from `i` on that is not a flag or a flag's value."""
    while i < len(words) and words[i].startswith("-") and words[i] != "-":
        i += 2 if words[i] in value_flags else 1
    return i


def _unwrapped(stage):
    """`(words, problem)`: the command `stage` runs, past keywords, wrappers and assignments.

    `problem` is set when the stage's own prefix already writes or cannot be judged: `sudo`,
    `env -S`, a variable outside `SAFE_ASSIGNMENTS`, or `uv` other than `uv run`.
    """
    if stage[:1] in (["command"], ["type"]) and {"-v", "-V"} & set(stage[1:]):
        # `command -v jq` looks a name up; `strip_shell_keywords` drops `command` itself.
        return [], None
    words = list(strip_shell_keywords(stage))
    if words and words[0].startswith("("):
        words[0] = words[0].lstrip("(")
        words = [w for w in words[:1] if w] + words[1:]
    if words and words[-1].endswith(")"):
        words[-1] = words[-1].rstrip(")")
    i = 0
    while i < len(words):
        word = words[i]
        name = word
        assignment = commands.ASSIGNMENT.match(word)
        if assignment:
            if assignment.group(1) not in commands.SAFE_ASSIGNMENTS:
                return (
                    [],
                    f"sets `{assignment.group(1)}`, which can make a program run a command",
                )
            i += 1
        elif name in ("else", "{", "!"):
            i += 1
        elif name in ("sudo", "doas"):
            return [], f"runs `{name}`"
        elif name in ("nohup", "time", "command", "builtin", "exec", "stdbuf"):
            i = _skip_flags(words, i + 1)
        elif name == "nice":
            i = _skip_flags(words, i + 1, frozenset({"-n"}))
        elif name == "timeout":
            i = _skip_flags(words, i + 1, _TIMEOUT_VALUE_FLAGS) + 1
        elif name == "env":
            if {"-S", "--split-string"} & {w.split("=", 1)[0] for w in words[i + 1 :]}:
                return [], "runs `env -S`, which splits a string into a command"
            i = _skip_flags(words, i + 1, frozenset({"-u", "--unset", "-C", "--chdir"}))
        elif name == "xargs":
            i = _skip_flags(words, i + 1, _XARGS_VALUE_FLAGS)
        elif name == "uv":
            if words[i + 1 : i + 2] != ["run"]:
                return [], f"runs `uv {' '.join(words[i + 1 : i + 2])}`"
            i = _skip_flags(words, i + 2, _UV_RUN_VALUE_FLAGS)
            if words[i : i + 1] == ["--"]:
                i += 1
        else:
            break
    return words[i:], None


def _ssh_problem(args, repo_root, written_paths, depth):
    for i, arg in enumerate(args):
        option = args[i + 1] if arg == "-o" and i + 1 < len(args) else arg[2:]
        if (arg == "-o" or arg.startswith("-o")) and re.match(
            r"(?i)\s*(proxycommand|localcommand|permitlocalcommand|proxyjump)", option
        ):
            return "passes an ssh option that runs a local command"
        if arg == "-F" or arg.startswith("-F"):
            return "reads an ssh config file the command names"
    found = commands._positionals(args, _SSH_VALUE_FLAGS)
    if len(found) < 2:
        return "opens an interactive ssh session"
    remote = " ".join(found[1:])
    # The remote shell has no session cwd, so every relative path there is non-scratch.
    problem = read_only_reason(remote, "/", written_paths, depth + 1)
    return f"runs on `{found[0]}` a command that {problem}" if problem else None


def _python_problem(words, repo_root):
    args = words[1:]
    if {"-c", "-m"} & set(args) or not args:
        return "runs Python code this hook cannot read"
    found = commands._positionals(args, frozenset({"-W", "-X"}))
    if not found or found[0] == "-":
        return "runs Python code this hook cannot read"
    script = found[0]
    rest = args[args.index(script) + 1 :]
    return commands.script_problem(script, rest, repo_root, _OWN_CHECKOUT)


def _stage_problem(stage, repo_root, written_paths, depth):
    """Why this one stage writes, or None."""
    words, problem = _unwrapped(stage)
    if problem or not words:
        return problem
    word = words[0]
    if word.startswith(_SYSTEM_BIN):
        word = os.path.basename(word)
    if word in ("for", "case", "done", "fi", "esac", "}", "in"):
        return None
    if word in commands.SHELLS or os.path.basename(word) in commands.SHELLS:
        return f"runs `{word}`, whose command text this hook does not judge"
    if word == "git":
        return _git_problem(words)
    if word == "gh":
        return _gh_problem(words)
    if word == "ssh":
        return _ssh_problem(words[1:], repo_root, written_paths, depth)
    if re.fullmatch(r"python3?(\.\d+)?", word):
        return _python_problem(words, repo_root)
    if word.endswith(".py"):
        return commands.script_problem(word, words[1:], repo_root, _OWN_CHECKOUT)
    check = commands.checks(repo_root, _is_scratch).get(word)
    if check is None:
        return f"runs `{word}`, which is not in the read-only command set"
    return check(words[1:])


_VARIABLE_REDIRECT = re.compile(r"(?<![<])>[>|]?\s*[\"']?\$")
# Deeper than any real command nests; past it the text is refused rather than recursed into.
_MAX_DEPTH = 4


def read_only_reason(command, repo_root, written_paths, depth=0):
    """A deny reason if this command writes, from an agent that declared it writes nothing.

    Args:
        command: the Bash text.
        repo_root: the directory a relative path resolves against.
        written_paths: block-protected-bash's redirect detector, passed in to keep it one
            function.
        depth: how many `$( )` bodies or `ssh` remote commands deep this text is.
    """
    if depth > _MAX_DEPTH:
        return "nests commands deeper than this hook reads"
    for path in written_paths(command):
        if not _is_scratch(path, repo_root):
            return f"redirects output into `{path}`"
    # `written_paths` drops a `$VAR/...` target, since it cannot resolve it. Here a target the
    # hook cannot resolve counts as a write: `> $PWD/CLAUDE.md` is one.
    if _VARIABLE_REDIRECT.search(command):
        return "redirects output into a path held in a shell variable"
    try:
        stages = split_stages(command)
        bodies = command_substitutions(command)
    except Unsplittable as exc:
        if exc.missing:
            # The redirect check above has already run; the verb checks need the segmenter.
            return None
        return f"cannot be read as shell ({exc.status}), so it cannot be shown to be a read"
    for stage in stages:
        problem = _stage_problem(stage, repo_root, written_paths, depth)
        if problem:
            return problem
    for body in bodies:
        problem = read_only_reason(body, repo_root, written_paths, depth + 1)
        if problem:
            return f"runs a substitution that {problem}"
    return None


def read_only_decision(
    payload, command, repo_root, written_paths, agents=read_only_agents
):
    """The `("deny", reason)` for a read-only agent's write, or None.

    Args:
        payload: the hook JSON; its `agent_type` names the subagent, absent for the main session.
        command: the Bash command text.
        repo_root: the directory a relative path resolves against.
        written_paths: block-protected-bash's redirect detector.
        agents: the read-only set's source, a test seam.
    """
    agent = payload.get("agent_type")
    if not agent or agent not in agents():
        return None
    problem = read_only_reason(command, repo_root, written_paths)
    if not problem:
        return None
    return (
        "deny",
        f"Agent `{agent}` is read-only (`read_only: true` in its frontmatter), and this "
        f"command {problem}. Report the change you would make instead of making it. A "
        f"redirect into /tmp is allowed, and the commands allowed are in "
        f"`.claude/hooks/hooklib/read_only_commands.py`.",
    )
