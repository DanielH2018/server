#!/usr/bin/env python3
"""PreToolUse(Bash) classifier: auto-approve provably read-only commands.

Reads the hook JSON on stdin. Prints a PreToolUse "allow" decision iff the WHOLE
command line is read-only: a single read-only command, or a pipeline whose every
stage is read-only. Anything else -> no output -> normal permission flow (an
allow-list match or an interactive prompt).

Safety model (deny by default):
  * Substitution is rejected outright -- $(...), backticks, ${...} -- because a
    quoted-looking argument can still expand/exec at the real shell.
  * Shell operators other than a plain pipe are rejected: ; & && || |& < > ( ).
    No chaining, redirection, backgrounding, or subshells. Tokenizing with
    shlex(punctuation_chars=True) makes each of these its OWN token, so a
    redirect or a glued `|rm` can never hide inside an argument.
  * Each stage's program must be on an allow-list of commands that cannot write
    or exec under ANY arguments (TIER1), OR pass a per-command guard that rejects
    the program's mutating forms (git, docker, sort, uniq, find, ip, systemctl,
    journalctl, rg).
Any failed check yields no output. The hook can only ever REDUCE prompts for safe
commands; it can never approve a write.
"""
import json
import re
import shlex
import sys

# --- Programs that cannot write or exec under ANY arguments --------------------
# Deliberately excludes commands with a write/exec mode: env (`env CMD`),
# less/more (`!cmd` escape), command/xargs/timeout/nice/... (exec wrappers),
# sed/awk (-i, system()), tee/dd/xxd/mount/stty (write), sort/uniq/find/ip/...
# (guarded below instead).
TIER1 = {
    # text / file readers and stdout-only filters (no output-file option)
    "ls", "cat", "head", "tail", "wc", "nl", "tac", "rev", "fold", "cut", "tr",
    "column", "comm", "grep", "egrep", "fgrep", "zgrep", "zcat", "od", "hexdump",
    "strings", "stat", "file", "readlink", "realpath", "basename", "dirname",
    "tree", "cksum", "md5sum", "sha1sum", "sha256sum", "sha512sum", "b2sum", "jq",
    # system / inspection
    "pwd", "whoami", "id", "groups", "hostname", "uname", "arch", "uptime",
    "date", "w", "who", "last", "lastlog", "df", "du", "free", "ps", "top",
    "vmstat", "iostat", "mpstat", "sar", "nproc", "lscpu", "lsblk", "lsusb",
    "lspci", "lsmod", "lsattr", "findmnt", "blkid", "getconf", "getent", "locale",
    "printenv", "lsof", "ss", "netstat", "dig", "host", "nslookup", "apt-cache",
    "echo", "printf", "seq", "true", "false", "which", "type",
}

# git subcommands that are read-only regardless of arguments (branch/tag/remote
# omitted: their bare form lists but `git branch <name>` / `-D` mutate).
GIT_READONLY = {
    "status", "log", "diff", "show", "describe", "rev-parse", "rev-list",
    "ls-files", "ls-tree", "blame", "shortlog", "whatchanged", "cat-file",
    "for-each-ref", "grep", "name-rev", "var",
}
# git global options safe to skip before the subcommand (NOT -c: config injection
# can set core.pager to an arbitrary command).
_GIT_SKIP = {"--no-pager", "-P", "--paginate", "--bare", "--literal-pathspecs",
             "--no-replace-objects", "--icase-pathspecs"}
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


_FIND_WRITE = {"-delete", "-exec", "-execdir", "-ok", "-okdir",
               "-fprint", "-fprintf", "-fprint0", "-fls"}


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


_IP_WRITE = {"add", "del", "delete", "set", "change", "replace", "flush",
             "append", "prepend", "save", "restore"}


def _ip(argv):
    return None if any(a in _IP_WRITE for a in argv[1:]) else "ip"


_SYSTEMCTL_WRITE = {
    "start", "stop", "restart", "reload", "reload-or-restart", "try-restart",
    "try-reload-or-restart", "enable", "disable", "reenable", "preset",
    "preset-all", "mask", "unmask", "link", "revert", "set-default", "isolate",
    "kill", "clean", "freeze", "thaw", "set-property", "edit", "daemon-reload",
    "daemon-reexec", "set-environment", "unset-environment", "import-environment",
    "reset-failed", "add-wants", "add-requires", "emergency", "rescue", "halt",
    "poweroff", "reboot", "suspend", "hibernate", "default", "switch-root",
}


def _systemctl(argv):
    return None if any(a in _SYSTEMCTL_WRITE for a in argv[1:]) else "systemctl"


_JOURNAL_WRITE = ("--rotate", "--vacuum-size", "--vacuum-time", "--vacuum-files",
                  "--flush", "--sync", "--relinquish-var",
                  "--smart-relinquish-var", "--update-catalog", "--setup-keys")


def _journalctl(argv):
    for a in argv[1:]:
        if any(a == p or a.startswith(p + "=") for p in _JOURNAL_WRITE):
            return None
    return "journalctl"


def _rg(argv):
    for a in argv[1:]:
        if a in ("--pre", "--hostname-bin") or a.startswith("--pre=") \
                or a.startswith("--hostname-bin="):
            return None  # --pre runs an arbitrary preprocessor command
    return "rg"


_DOCKER_READ = {"ps", "images", "inspect", "logs", "version", "info", "stats",
                "top", "port", "history", "events", "diff", "search", "df"}
_DOCKER_GROUP = {"network", "volume", "container", "image", "system", "node",
                 "service", "config", "context", "secret", "stack", "plugin"}
_DOCKER_GROUP_READ = {"ls", "inspect", "logs", "ps", "df", "top", "history",
                      "version", "events"}
_DOCKER_VALUE_FLAGS = {"-u", "--user", "-e", "--env", "-w", "--workdir", "-l",
                       "--label", "--env-file", "--detach-keys"}


def _docker_exec(rest):
    # rest = ['exec', <flags...>, <container>, <inner cmd> <args...>]
    i, n = 1, len(rest)
    while i < n and rest[i].startswith("-"):
        if rest[i] == "--":
            i += 1
            break
        i += 2 if rest[i] in _DOCKER_VALUE_FLAGS else 1
    inner = rest[i + 1:]  # skip the container name at rest[i]
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


HANDLERS = {
    "git": _git, "find": _find, "sort": _sort, "uniq": _uniq, "ip": _ip,
    "systemctl": _systemctl, "journalctl": _journalctl, "rg": _rg,
    "docker": _docker,
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


_SUBST = ("`", "$(", "${")
_OP_TOKEN = re.compile(r"[();<>&|]+\Z")  # a token made ENTIRELY of shell operators


def classify(command):
    """Return a reason string if the whole command line is read-only, else None."""
    if not command or "\n" in command or command.rstrip().endswith("\\"):
        return None
    if any(s in command for s in _SUBST):
        return None
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        return None
    if not tokens:
        return None
    # Reject any shell-operator token except a bare pipe (the only composition we
    # allow). With punctuation_chars, redirects/chaining/subshells are their own
    # tokens, so this catches them all.
    for t in tokens:
        if _OP_TOKEN.match(t) and t != "|":
            return None
    # Split into pipeline stages on '|'; every stage must be read-only.
    stages, cur = [], []
    for t in tokens:
        if t == "|":
            stages.append(cur)
            cur = []
        else:
            cur.append(t)
    stages.append(cur)
    reasons = []
    for stage in stages:
        if not stage:
            return None  # empty stage (leading/trailing/double pipe)
        r = _argv_readonly(stage)
        if not r:
            return None
        reasons.append(r)
    return "read-only: " + " | ".join(reasons)


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    command = ((data.get("tool_input") or {}).get("command")) or ""
    reason = classify(command)
    if reason:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "permissionDecisionReason": reason,
            }
        }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
