"""The commands a read-only agent may run through Bash, and the arguments that make one a write.

`read_only_agents.py` judges each stage of a read-only agent's command here, after the
redirect check and before git and gh, which it judges itself. The set is an allowlist: a
command it does not name is denied. A denylist of writers missed every command nobody had
listed (`sort -o`, `gzip`, `prek run`, `./scripts/deploy.sh`), and a wrapper or a nested shell
(`timeout 5 rm`, `uv run git push`, `bash -c '…'`) carried any listed writer past it.

DECIDED: allowlist, not denylist. A read the list lacks costs a read-only agent one rephrased
command, which the deny reason says how to do. A write a denylist lacks is the injected action
the arm exists to stop. The program text of `sed` and `awk` is read with a pattern that errs
toward a deny. `less` and `more` are allowed because an agent's Bash has no terminal for their
`!command` escape.

Every `check` function takes the argv after the command name and returns why it writes, or
None for a read.
"""

import os
import re
from collections.abc import Callable

# Assignments a read-only command may carry. Most other variables are inert, but `GIT_PAGER`,
# `GIT_EXTERNAL_DIFF`, `GIT_SSH_COMMAND`, `PAGER`, `LD_PRELOAD`, `BASH_ENV` and many more run a
# command they name, and an allowlist is shorter than that list.
SAFE_ASSIGNMENTS = frozenset(
    {"LC_ALL", "LANG", "LANGUAGE", "TZ", "NO_COLOR", "COLUMNS", "LINES", "TERM"}
)
ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=")

# The words that end or open a compound command and run nothing themselves.
CONTROL_WORDS = frozenset({"for", "case", "done", "fi", "esac", "}", "{", "in", "else"})

# Shells, and builtins that run text as a command. Re-segmenting their argument would judge it
# once, but the shell then re-reads it with its own quoting, so they are denied outright.
SHELLS = frozenset(
    {"bash", "sh", "zsh", "dash", "ksh", "fish", "eval", "source", ".", "su"}
)


def _short_letters(args):
    letters = set()
    for arg in args:
        if arg.startswith("-") and not arg.startswith("--") and len(arg) > 1:
            letters.update(arg[1:])
    return letters


def _long_keys(args):
    return {a.split("=", 1)[0] for a in args if a.startswith("--")}


def _positionals(args, value_flags=frozenset()):
    """The arguments that are not flags, skipping the value of each flag in `value_flags`."""
    out, i = [], 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            return out + args[i + 1 :]
        if arg in value_flags:
            i += 2
            continue
        if not arg.startswith("-") or arg == "-":
            out.append(arg)
        i += 1
    return out


def _flags(denied_short="", denied_long=()):
    """A check that refuses any of the given short letters or long options."""

    def check(args):
        short = set(denied_short) & _short_letters(args)
        long = set(denied_long) & _long_keys(args)
        if short or long:
            flag = sorted(long)[0] if long else f"-{sorted(short)[0]}"
            return f"passes `{flag}`, which writes a file or runs a command"
        return None

    return check


def _allowed(_args):
    return None


def _first_positional_in(allowed, value_flags=frozenset()):
    def check(args):
        found = _positionals(args, value_flags)
        verb = found[0] if found else ""
        return None if verb in allowed else f"runs subcommand `{verb}`"

    return check


def _find(args):
    writers = {
        "-exec",
        "-execdir",
        "-ok",
        "-okdir",
        "-delete",
        "-fprint",
        "-fprint0",
        "-fprintf",
        "-fls",
    }
    hit = writers & set(args)
    return f"passes `{sorted(hit)[0]}`" if hit else None


def _uniq(args):
    # `uniq IN OUT` writes OUT.
    return (
        "names an output file"
        if len(_positionals(args, {"-f", "-s", "-w"})) > 1
        else None
    )


def _curl(args):
    problem = _flags(
        "oOTdFKcD",
        (
            "--output",
            "--remote-name",
            "--remote-name-all",
            "--upload-file",
            "--data",
            "--data-raw",
            "--data-binary",
            "--data-urlencode",
            "--data-ascii",
            "--form",
            "--form-string",
            "--json",
            "--config",
            "--cookie-jar",
            "--dump-header",
            "--trace",
            "--trace-ascii",
            "--stderr",
            "--libcurl",
            "--output-dir",
        ),
    )(args)
    if problem:
        return problem
    for i, arg in enumerate(args):
        method = None
        if arg in ("-X", "--request"):
            method = args[i + 1] if i + 1 < len(args) else ""
        elif arg.startswith("--request="):
            method = arg.split("=", 1)[1]
        elif arg.startswith("-X") and len(arg) > 2:
            method = arg[2:]
        if method is not None and method.upper() not in ("GET", "HEAD"):
            return f"sends a {method or 'missing'} request"
    return None


# A `sed` command that writes (`w`, `W`) or runs one (`e`), as a command or as an `s///` flag.
_SED_WRITE = re.compile(
    r"(?:^|[;{}\n])\s*(?:[0-9$,]+|/[^/]*/)?\s*!?\s*[wWe](?:\s|$)"
    r"|s(.).*?\1.*?\1[gpiImM0-9]*[we]"
)


def _sed(args):
    problem = _flags("if", ("--in-place", "--file"))(args)
    if problem:
        return problem
    scripts = [
        args[i + 1] for i, a in enumerate(args[:-1]) if a in ("-e", "--expression")
    ]
    if not scripts:
        found = _positionals(args)
        scripts = found[:1]
    for script in scripts:
        if _SED_WRITE.search(script):
            return "runs a sed program that writes a file or runs a command"
    return None


# `system()`, a pipe to or from a command, and a `print >` redirect.
_AWK_WRITE = re.compile(
    r"system\s*\(|\|\s*(?:getline|\"|\w)|\"\s*\||\bprintf?\b[^;}]*>"
)


def _awk(args):
    problem = _flags("ifE", ("--include", "--file", "--exec", "--load"))(args)
    if problem:
        return problem
    found = _positionals(args, {"-v", "-F"})
    if found and _AWK_WRITE.search(found[0]):
        return "runs an awk program that writes a file or runs a command"
    return None


_KUBECTL_READS = frozenset(
    {
        "get",
        "describe",
        "logs",
        "top",
        "explain",
        "api-resources",
        "api-versions",
        "version",
        "events",
        "auth",
        "cluster-info",
        "wait",
    }
)
_KUBECTL_VALUE_FLAGS = frozenset(
    {
        "-n",
        "--namespace",
        "-l",
        "--selector",
        "-o",
        "--output",
        "-c",
        "--container",
        "--context",
    }
)


def _kubectl(args):
    identity = {
        "--kubeconfig",
        "--token",
        "--as",
        "--as-group",
        "--username",
        "--password",
    }
    if identity & _long_keys(args):
        return "changes the identity kubectl authenticates as"
    found = _positionals(args, _KUBECTL_VALUE_FLAGS)
    verb = found[0] if found else ""
    if verb in _KUBECTL_READS:
        return None
    sub = found[1] if len(found) > 1 else ""
    if verb == "rollout" and sub in ("status", "history"):
        return None
    if verb == "config" and sub in ("view", "current-context", "get-contexts"):
        return None
    return f"runs `kubectl {verb}`, which is not in the read-only set"


_SYSTEMCTL_READS = frozenset(
    {
        "status",
        "show",
        "cat",
        "list-units",
        "list-timers",
        "list-unit-files",
        "list-dependencies",
        "list-sockets",
        "is-active",
        "is-enabled",
        "is-failed",
        "is-system-running",
    }
)

_DOCKER_READS = frozenset(
    {"ps", "logs", "inspect", "images", "stats", "version", "info", "top", "port"}
)


def _docker(args):
    found = _positionals(
        args, {"-H", "--host", "--context", "-f", "--filter", "--format"}
    )
    if found[:1] == ["compose"]:
        return (
            None
            if found[1:2] in (["ps"], ["logs"], ["config"])
            else "runs `docker compose`"
        )
    if found[:1] and found[0] in _DOCKER_READS:
        return None
    return f"runs `docker {found[0] if found else ''}`"


def _ip(args):
    writers = {
        "add",
        "del",
        "delete",
        "change",
        "replace",
        "set",
        "flush",
        "append",
        "exec",
    }
    hit = writers & set(args)
    return f"runs `ip … {sorted(hit)[0]}`" if hit else None


def _tee_check(repo_root, is_scratch):
    def check(args):
        for target in _positionals(args):
            if not is_scratch(target, repo_root):
                return f"tees into `{target}`"
        return None

    return check


# Scripts in this tree that only read, with the subcommands that only read. `None` allows every
# subcommand: `probe.py` is the read-only diagnostics entry point by design.
READ_SCRIPTS = {
    "scripts/diagnostics/probe.py": None,
    "scripts/dev/fact_status.py": frozenset({"status", "lint"}),
    "scripts/dev/findings.py": frozenset(
        {"list", "show", "history", "claims", "next", "verify"}
    ),
}


def script_problem(script, args, repo_root, own_checkout):
    """Why running the Python file `script` with `args` writes, or None."""
    path = os.path.realpath(os.path.join(repo_root, script))
    for rel, subcommands in READ_SCRIPTS.items():
        known = {
            os.path.realpath(os.path.join(root, rel))
            for root in (repo_root, own_checkout)
        }
        if path not in known and not path.endswith("/" + rel):
            continue
        found = _positionals(args, {"--repo"})
        if subcommands is None or (found and found[0] in subcommands):
            return None
        return f"runs `{rel} {found[0] if found else ''}`, which is not a read"
    return f"runs `{script}`, which is not a known read-only script"


def checks(repo_root, is_scratch):
    """Command name -> its check. A command absent from the mapping is not a read."""
    plain = (
        "cat head tail less more grep egrep fgrep wc cut tr paste join comm tac nl fold fmt "
        "expand unexpand rev column seq basename dirname realpath readlink pwd printf echo "
        "true false test [ [[ ]] ls tree stat file du df diff cmp sha256sum sha1sum md5sum "
        "base64 od hexdump strings jq id whoami hostname uname uptime free ps pgrep nproc "
        "lscpu lsblk which type date printenv ss netstat dig nslookup host ping traceroute "
        "tracepath getent cd :"
    ).split()
    table: dict[str, Callable[[list[str]], str | None]] = {
        name: _allowed for name in plain
    }
    table.update(
        {
            "find": _find,
            "fd": _flags("xX", ("--exec", "--exec-batch")),
            "rg": _flags("", ("--pre",)),
            "sort": _flags("o", ("--output", "--compress-program")),
            "uniq": _uniq,
            "sed": _sed,
            "awk": _awk,
            "gawk": _awk,
            "mawk": _awk,
            "yq": _flags("i", ("--inplace",)),
            "bat": _flags("", ("--pager",)),
            "curl": _curl,
            "openssl": lambda a: (
                "writes a file" if {"-out", "-keyout", "-sess_out"} & set(a) else None
            ),
            "kubectl": _kubectl,
            "systemctl": _first_positional_in(_SYSTEMCTL_READS),
            "docker": _docker,
            "ip": _ip,
            "tee": _tee_check(repo_root, is_scratch),
        }
    )
    # A journal write needs root, but these flags are journalctl's own writes.
    table["journalctl"] = _flags(
        "",
        (
            "--vacuum-size",
            "--vacuum-time",
            "--vacuum-files",
            "--rotate",
            "--flush",
            "--sync",
            "--relinquish-var",
            "--setup-keys",
        ),
    )
    return table
