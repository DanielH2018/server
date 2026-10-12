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
    git_invocation,
    split_stages,
    strip_shell_keywords,
)

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
# arm exists to stop. The arm is still a reading of command text: a write inside `python3 -c`,
# a called script or an `ssh` remote command is not seen, nor are `curl -o`, `wget -O`,
# `tar -x`, `find -delete`/`-exec` or `cp -t <dir>`. kubectl's read-only ServiceAccount covers
# the cluster on its own.

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
    # `realpath`, not `normpath`: `ln -s <repo file> /tmp/l` then `> /tmp/l` writes the repo.
    target = os.path.realpath(os.path.join(repo_root, os.path.expanduser(path)))
    return (target + "/").startswith(_SCRATCH_PREFIXES)


# Commands that change a file named in their arguments. For a copy the destination is the last
# argument, so `cp ansible/x /tmp/y` stays a read of the repo.
_FILE_MUTATORS = {
    "rm",
    "rmdir",
    "unlink",
    "shred",
    "mv",
    "touch",
    "mkdir",
    "chmod",
    "chown",
    "chgrp",
    "truncate",
    "patch",
}
_FILE_COPIERS = {"cp", "install", "ln", "rsync", "scp"}
_WRAPPER_WORDS = {"sudo", "env", "xargs", "nohup", "command", "exec", "nice", "time"}

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
    args = list(inv.args)
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


def _unwrapped(stage):
    """`stage` from the command it actually runs: past keywords, assignments and wrappers."""
    words = strip_shell_keywords(stage)
    i = 0
    while i < len(words):
        word = words[i]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", word) or word.startswith("-"):
            i += 1
        elif os.path.basename(word) in _WRAPPER_WORDS:
            i += 1
        else:
            break
    return words[i:]


def _stage_problem(stage, repo_root):
    """Why this one stage writes, or None."""
    words = _unwrapped(stage)
    if not words:
        return None
    name = os.path.basename(words[0])
    if name == "git":
        problem = _git_problem(stage)
        if problem:
            return problem
    if name == "gh":
        return _gh_problem(words)
    targets = [w for w in words[1:] if not w.startswith("-")]
    if name in _FILE_COPIERS:
        targets = targets[-1:]
    elif name == "dd":
        targets = [w[3:] for w in words[1:] if w.startswith("of=")]
    elif name not in _FILE_MUTATORS:
        return None
    for target in targets:
        if not _is_scratch(target, repo_root):
            return f"runs `{name}` on `{target}`"
    return None


_VARIABLE_REDIRECT = re.compile(r"(?<![<])>>?\s*[\"']?\$")


def read_only_reason(command, repo_root, written_paths):
    """A deny reason if this command writes, from an agent that declared it writes nothing.

    `written_paths` is block-protected-bash's redirect detector, passed in to keep it one
    function.
    """
    for path in written_paths(command):
        if not _is_scratch(path, repo_root):
            return f"redirects output into `{path}`"
    # `written_paths` drops a `$VAR/...` target, since it cannot resolve it. Here a target the
    # hook cannot resolve counts as a write: `> $PWD/CLAUDE.md` is one.
    if _VARIABLE_REDIRECT.search(command):
        return "redirects output into a path held in a shell variable"
    try:
        stages = split_stages(command)
    except Unsplittable as exc:
        if exc.missing:
            # The redirect check above has already run; the verb checks need the segmenter.
            return None
        return f"cannot be read as shell ({exc.status}), so it cannot be shown to be a read"
    for stage in stages:
        problem = _stage_problem(stage, repo_root)
        if problem:
            return problem
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
        f"command {problem}. Report the change you would make instead of making it; a "
        f"scratch file under /tmp is allowed.",
    )
