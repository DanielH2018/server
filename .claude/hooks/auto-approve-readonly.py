#!/usr/bin/env python3
# gen-hooks: library
#   reason: run by auto-approve-readonly.sh through `uv run python`
"""Bash classifier: auto-approve provably read-only commands.

Reads the hook JSON on stdin. Prints a PreToolUse "allow" decision iff the WHOLE command line is
read-only: a single read-only command, or a pipeline whose every stage is read-only. Anything else
-> no output -> normal permission flow (an allow-list match or an interactive prompt).

Safety model (deny by default):
  * Substitution is rejected outright -- $(...), backticks, ${...} -- because a
    quoted-looking argument can still expand/exec at the real shell.
  * Stages come from the dotfiles segmenter the deny guards also read
    (`_hook_common.segments`, #2198): a quoted `;` stays in its word, a heredoc
    body is lifted off the text. No segmenter, or text it refuses, is no verdict.
  * Within a stage, shlex(punctuation_chars=True) makes every operator its OWN
    token, so a redirect, a paren or a glued `|rm` never hides in an argument.
    Backgrounding, subshells and writes to a real file are rejected.
  * A heredoc stage is rejected: the body reaches the program's stdin (`ssh host
    <<EOF` runs it), and the shared corpus pins `cat <<EOF` as not read-only, so
    widening to TIER1 programs is a corpus change in the dotfiles repo first.
  * Each stage's program must be on an allow-list of commands that cannot write
    or exec under ANY arguments (TIER1), OR pass a per-command guard that rejects
    the program's mutating forms (git, docker, sort, uniq, find, ip, systemctl,
    journalctl, rg).
The hook can only ever REDUCE prompts for safe commands; it can never approve a write.
"""

import json
import re
import shlex
import sys

from _hook_common import (
    Unsplittable,
    _deployed_parse,
    emit_pretooluse_decision,
    segments,
)

# DECIDED: the underscore-prefixed names below cross a module boundary on purpose. The tables
# and the tokenizer moved out of this file byte-for-byte, changing no verdict; making them
# public would have turned that move into a rewrite of a security boundary. The underscore
# still carries what it did before — internal to this classifier, not an API another hook may
# import. Conventions for a new module: docs/python-code-organization.md.
from _readonly_sed import _sed
from _readonly_shell import (
    _FORBIDDEN,
    _OP_TOKEN,
    _STAGE_SEPS,
    _SUBST,
    _strip_redirects,
)
from _readonly_tables import (
    SSH_HOSTS,
    TIER1,
    _FLAG_MUTATES,
    _JOURNAL_WRITE,
    _SSH_FLAGS,
    _SSH_GLOB,
    _SSH_OPTIONS,
    _SSH_SECRET,
    _SSH_VALUE_FLAGS,
)

# git subcommands that are read-only regardless of arguments (branch/tag/remote
# omitted: their bare form lists but `git branch <name>` / `-D` mutate).
GIT_READONLY = {
    "status",
    "log",
    "diff",
    "show",
    "describe",
    "rev-parse",
    "rev-list",
    "ls-files",
    "ls-tree",
    "blame",
    "shortlog",
    "whatchanged",
    "cat-file",
    "for-each-ref",
    "grep",
    "name-rev",
    "var",
}
# git global options safe to skip before the subcommand (NOT -c: config injection
# can set core.pager to an arbitrary command).
_GIT_SKIP = {
    "--no-pager",
    "-P",
    "--paginate",
    "--bare",
    "--literal-pathspecs",
    "--no-replace-objects",
    "--icase-pathspecs",
}
_GIT_SKIP_VALUE = {"-C", "--git-dir", "--work-tree", "--namespace", "--super-prefix"}


def _git(argv):
    i, n = 1, len(argv)
    while i < n and argv[i].startswith("-"):
        a = argv[i]
        if a in _GIT_SKIP:
            i += 1
        elif a in _GIT_SKIP_VALUE:
            i += 2
        elif a.split("=", 1)[0] in _GIT_SKIP_VALUE:
            i += 1
        else:
            return None  # -c and anything unrecognised: reject
    if i < n and argv[i] in GIT_READONLY:
        return "git " + argv[i]
    return None


_FIND_WRITE = {
    "-delete",
    "-exec",
    "-execdir",
    "-ok",
    "-okdir",
    "-fprint",
    "-fprintf",
    "-fprint0",
    "-fls",
}


def _find(argv):
    return None if any(a in _FIND_WRITE for a in argv[1:]) else "find"


def _sort(argv):
    for a in argv[1:]:
        if a == "--output" or a.startswith("--output=") or re.match(r"-[A-Za-z]*o", a):
            return None  # -o / --output writes to a file
    return "sort"


def _uniq(argv):
    # uniq [INPUT [OUTPUT]] -- a 2nd positional is an output file (write).
    pos = [a for a in argv[1:] if not a.startswith("-")]
    return "uniq" if len(pos) <= 1 else None


_IP_WRITE = {
    "add",
    "del",
    "delete",
    "set",
    "change",
    "replace",
    "flush",
    "append",
    "prepend",
    "save",
    "restore",
}


def _ip(argv):
    return None if any(a in _IP_WRITE for a in argv[1:]) else "ip"


_SYSTEMCTL_WRITE = {
    "start",
    "stop",
    "restart",
    "reload",
    "reload-or-restart",
    "try-restart",
    "try-reload-or-restart",
    "enable",
    "disable",
    "reenable",
    "preset",
    "preset-all",
    "mask",
    "unmask",
    "link",
    "revert",
    "set-default",
    "isolate",
    "kill",
    "clean",
    "freeze",
    "thaw",
    "set-property",
    "edit",
    "daemon-reload",
    "daemon-reexec",
    "set-environment",
    "unset-environment",
    "import-environment",
    "reset-failed",
    "add-wants",
    "add-requires",
    "emergency",
    "rescue",
    "halt",
    "poweroff",
    "reboot",
    "suspend",
    "hibernate",
    "default",
    "switch-root",
}


def _systemctl(argv):
    return None if any(a in _SYSTEMCTL_WRITE for a in argv[1:]) else "systemctl"


def _journalctl(argv):
    for a in argv[1:]:
        if any(a == p or a.startswith(p + "=") for p in _JOURNAL_WRITE):
            return None
    return "journalctl"


def _rg(argv):
    # --pre runs an arbitrary preprocessor per file, --hostname-bin an arbitrary binary.
    # Same shape as the package's `rg_readonly`, which the shared-verdict replay compares.
    exec_flags = ("--pre", "--hostname-bin")
    return None if any(a.split("=", 1)[0] in exec_flags for a in argv[1:]) else "rg"


_DOCKER_READ = {
    "ps",
    "images",
    "inspect",
    "logs",
    "version",
    "info",
    "stats",
    "top",
    "port",
    "history",
    "events",
    "diff",
    "search",
    "df",
}
_DOCKER_GROUP = {
    "network",
    "volume",
    "container",
    "image",
    "system",
    "node",
    "service",
    "config",
    "context",
    "secret",
    "stack",
    "plugin",
}
_DOCKER_GROUP_READ = {
    "ls",
    "inspect",
    "logs",
    "ps",
    "df",
    "top",
    "history",
    "version",
    "events",
}
_DOCKER_VALUE_FLAGS = {
    "-u",
    "--user",
    "-e",
    "--env",
    "-w",
    "--workdir",
    "-l",
    "--label",
    "--env-file",
    "--detach-keys",
}


def _docker_exec(rest):
    # rest = ['exec', <flags...>, <container>, <inner cmd> <args...>]
    i, n = 1, len(rest)
    while i < n and rest[i].startswith("-"):
        if rest[i] == "--":
            i += 1
            break
        i += 2 if rest[i] in _DOCKER_VALUE_FLAGS else 1
    inner = rest[i + 1 :]  # skip the container name at rest[i]
    base = _argv_readonly(inner)
    return ("docker exec " + base) if base else None


def _docker(argv):
    rest = argv[1:]
    if not rest:
        return None
    if rest[0] == "exec":
        return _docker_exec(rest)
    if rest[0] in _DOCKER_READ:
        return "docker " + rest[0]
    if rest[0] in _DOCKER_GROUP and len(rest) >= 2 and rest[1] in _DOCKER_GROUP_READ:
        return "docker %s %s" % (rest[0], rest[1])
    return None


# awk can write files (`print > "f"`), pipe to a shell (`print | "sh"`,
# `"cmd" | getline`) or exec (`system(...)`); -f reads an uninspectable program
# and -i edits in place. Reject the program outright if any of these appear, and
# refuse -f/-i. The `>` check also rejects benign comparisons -- safe over-reject.
_AWK_DANGER = ("system", "getline", "|", ">")


def _awk(argv):
    prog = []
    i, n = 1, len(argv)
    while i < n:
        a = argv[i]
        if a == "--":
            i += 1
            break
        if not a.startswith("-") or a == "-":
            prog.append(a)  # first positional is the program text
            i += 1
            break
        if a.startswith("-f") or a.startswith("-i"):
            return None  # -f program-file (uninspectable), -i in-place
        if a in ("-e", "--source"):
            if i + 1 >= n:
                return None
            prog.append(argv[i + 1])
            i += 2
            continue
        if a in ("-v", "-F"):
            i += 2  # option takes a separate value
            continue
        i += 1  # other/glued flags (-F:, -vX=1, -W ...)
    text = " ".join(prog)
    if not text or any(d in text for d in _AWK_DANGER):
        return None
    return "awk"


# package-manager / host-query guards
# Same binaries query read-only but mutate under install/remove/etc. actions, so
# each is gated to its read-only forms (deny by default). The always-read-only
# query tools (apt-cache, dpkg-query, lsb_release, mailq) live in TIER1 instead.

# dpkg: an action flag selects the mode. Approve only when a read-only action is
# present and no mutating action (-i/--install, -r/--remove, -P/--purge,
# --configure, --unpack, --set-selections, ...) appears.
_DPKG_READ = {
    "-l",
    "--list",
    "-L",
    "--listfiles",
    "-s",
    "--status",
    "-p",
    "--print-avail",
    "-S",
    "--search",
    "-V",
    "--verify",
    "-C",
    "--audit",
    "--get-selections",
    "--print-architecture",
    "--print-foreign-architectures",
}
_DPKG_WRITE = {
    "-i",
    "--install",
    "--unpack",
    "--configure",
    "-r",
    "--remove",
    "-P",
    "--purge",
    "-A",
    "--record-avail",
    "--update-avail",
    "--merge-avail",
    "--clear-avail",
    "--set-selections",
    "--clear-selections",
    "--forget-old-unavail",
    "--add-architecture",
    "--remove-architecture",
    "--triggers-only",
}


def _dpkg(argv):
    saw_read = False
    for a in argv[1:]:
        key = a.split("=", 1)[0]
        if key in _DPKG_WRITE:
            return None
        if key in _DPKG_READ:
            saw_read = True
    return "dpkg" if saw_read else None


# apt: the first non-option token is the subcommand; approve only query ones.
_APT_READ = {
    "list",
    "show",
    "search",
    "policy",
    "depends",
    "rdepends",
    "showsrc",
    "madison",
    "moo",
}
_APT_SKIP_VALUE = {"-o", "--option", "-c", "--config-file", "-t", "--target-release"}


def _apt(argv):
    i, n = 1, len(argv)
    while i < n:
        a = argv[i]
        if a in _APT_SKIP_VALUE:
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        return ("apt " + a) if a in _APT_READ else None
    return None


_APT_MARK_READ = {"showmanual", "showauto", "showhold", "showinstall"}


def _apt_mark(argv):
    for a in argv[1:]:
        if a.startswith("-"):
            continue
        return ("apt-mark " + a) if a in _APT_MARK_READ else None
    return None


_PIPX_READ = {"list", "environment"}


def _pipx(argv):
    for a in argv[1:]:
        if a == "--version":
            return "pipx --version"
        if a.startswith("-"):
            continue
        return ("pipx " + a) if a in _PIPX_READ else None
    return None


def _crontab(argv):
    # Read-only ONLY as `crontab -l`. `-r` deletes, `-e`/`-i` edit, and a bare file
    # argument (or bare `crontab` reading stdin) installs a new crontab.
    saw_list = False
    i, n = 1, len(argv)
    while i < n:
        a = argv[i]
        if a == "-l":
            saw_list = True
            i += 1
        elif a == "-u":
            i += 2  # -u USER takes a value
        else:
            return None
    return "crontab -l" if saw_list else None


def _flag_guarded(argv):
    # ss, dmesg and sensors read except under a few flags, named per verb in `_FLAG_MUTATES`
    # as long options (matched before any `=`) plus the letters that hide in a short cluster
    # (`-xKy`, `-xCy`). #2052, and the package's copy is `remote_guards._flag_guarded` (#2078).
    name = argv[0].rsplit("/", 1)[-1]
    longs, letters = _FLAG_MUTATES[name]
    cluster = re.compile(rf"-[a-zA-Z]*[{letters}]")
    mutates = any(a.split("=", 1)[0] in longs or cluster.match(a) for a in argv[1:])
    return None if mutates else name


def _ssh(argv):
    # `ssh [opts] [user@]host CMD...` where CMD is itself read-only. ssh joins its
    # remaining arguments with spaces and hands the result to the remote shell, so
    # reconstructing the string and re-running classify() on it is exactly what the
    # far side sees -- and it reuses every guard the local path already has.
    i, n = 1, len(argv)
    while i < n:
        a = argv[i]
        if a in _SSH_FLAGS:
            i += 1
        elif a in _SSH_VALUE_FLAGS:
            if i + 1 >= n:
                return None
            if a == "-o" and _ssh_option_key(argv[i + 1]) not in _SSH_OPTIONS:
                return None
            i += 2
        elif a.startswith("-"):
            return None  # unknown flag, or a glued form we can't read
        else:
            break
    if i >= n:
        return None
    host = argv[i].split("@", 1)[-1]
    if host not in SSH_HOSTS:
        return None
    remote = " ".join(argv[i + 1 :])
    if not remote.strip():
        return None  # no command means an interactive shell
    if remote.split()[0].rsplit("/", 1)[-1] == "ssh":
        return None  # a second hop is a shape worth confirming, not recursing into
    if _SSH_SECRET.search(remote) or _SSH_GLOB.search(remote):
        return None
    return "ssh {}".format(host) if classify(remote) else None


def _ssh_option_key(opt):
    # `-o BatchMode=yes` and `-o "BatchMode yes"` are both accepted by ssh.
    return re.split(r"[=\s]", opt.strip(), maxsplit=1)[0].lower()


HANDLERS = {
    "ssh": _ssh,
    "git": _git,
    "find": _find,
    "sort": _sort,
    "uniq": _uniq,
    "ip": _ip,
    "systemctl": _systemctl,
    "journalctl": _journalctl,
    "rg": _rg,
    "docker": _docker,
    "awk": _awk,
    "gawk": _awk,
    "mawk": _awk,
    "sed": _sed,
    "dpkg": _dpkg,
    "apt": _apt,
    "apt-mark": _apt_mark,
    "pipx": _pipx,
    "crontab": _crontab,
    "sensors": _flag_guarded,
    "ss": _flag_guarded,
    "dmesg": _flag_guarded,
}


def _argv_readonly(argv):
    """Return a reason string if argv (one command + args) is read-only, else None."""
    if not argv:
        return None
    name = argv[0].rsplit("/", 1)[-1]
    if name in TIER1:
        return name
    handler = HANDLERS.get(name)
    return handler(argv) if handler else None


def classify(command, parse=_deployed_parse):
    """Return a reason string if the whole command line is read-only, else None.

    The command may be a sequence (`;`, `&&`, `||`, or newlines) of pipelines;
    every stage of every pipeline must be read-only. Substitution, subshells,
    backgrounding, and writes to real files are rejected outright. A test hands
    `parse=None` to stand on the undeployed host: no segmenter, no verdict.
    """
    stripped = command.rstrip()
    if not stripped or stripped.endswith("\\"):
        return None
    if any(s in command for s in _SUBST):
        return None
    try:
        segs = segments(command, parse)
    except Unsplittable:
        return None  # no segmenter, or text it refused: never an allow
    reasons = []
    for seg in segs:
        if seg.sep == "&":
            # The trailing form too: the collapse keeps the `&` on the survivor (#2261).
            return None
        try:
            lex = shlex.shlex(seg.text, posix=True, punctuation_chars=True)
            lex.whitespace_split = True
            stage = list(lex)
        except ValueError:
            return None
        if not stage:
            return None  # empty stage (e.g. ';;' or a dangling operator)
        if any(tok in _FORBIDDEN or tok in _STAGE_SEPS for tok in stage):
            return None  # subshell, backgrounding, or an uncut separator
        argv = _strip_redirects(stage)
        if argv is None:
            return None
        if not argv or any(_OP_TOKEN.match(tok) for tok in argv):
            return None  # redirect-only stage / stray operator
        if seg.heredocs:
            return None  # see the module docstring
        r = _argv_readonly(argv)
        if not r:
            return None
        reasons.append(r)
    if not reasons:
        return None
    return "read-only: " + " | ".join(reasons)


def main():
    """Read the hook payload from stdin and emit an allow decision for a read-only command.

    Allows a PreToolUse command whenever `classify` recognizes it as read-only. Emits
    nothing, and always exits 0, when no rule matches.

    # DECIDED (#1864, #1898): this file no longer answers PermissionRequest. The
    # `--permission-request` entry point and its `auto-approve-remote-ssh.sh` shim stayed
    # beside the user-level judge hook for one day (2026-09-18) because they walked a local
    # pipeline around an ssh stage and guarded git/sed/awk/find/... over ssh, and the judge
    # did neither. Both moved into the dotfiles package that day (dotfiles PR #521:
    # `judge_segment`'s ssh/hl arm, `checks/remote_guards.py`), so the shim retired. The
    # `_ssh` handler above stays: it serves THIS entry point, the PreToolUse one.
    # docs/claude-shell-permissions.md has the long form.
    """
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    command = ((data.get("tool_input") or {}).get("command")) or ""
    reason = classify(command)
    if reason:
        emit_pretooluse_decision("allow", reason)
    return 0


if __name__ == "__main__":
    sys.exit(main())
