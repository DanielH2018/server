#!/usr/bin/python3 -I
"""homelab-rootq — read root-only host state, run as root through one NOPASSWD line (#6323).

Installed as /usr/local/sbin/homelab-rootq by tasks/rootq.yml; /usr/local/bin/rootq runs it
through `sudo -n`. It reads and never writes, and each verb runs a fixed command:

  rootq nft             nft list ruleset
  rootq ufw             ufw status verbose
  rootq sshd            sshd -T
  rootq crontab         crontab -l -u root
  rootq ls PATH         ls -la -- PATH
  rootq stat PATH       stat -- PATH
  rootq cat PATH        the file, read here rather than by cat

No flag or other argument reaches a command. PATH is absolute, and it must resolve to itself:
a symlink anywhere along it, `..`, `.`, `//` or a trailing slash is refused. It must sit under
one of the directories in ALLOW, every directory above it must be root-owned and writable by
root alone, and it must miss every pattern in DENY. `cat` also refuses a file with more than
one hard link, a binary file and a file over READ_MAX bytes.

Output is filtered twice. Output that matches a pattern in REFUSE is withheld whole, so a file
or crontab holding a key, a token or a webhook prints nothing. Otherwise, in every `NAME=value`
assignment the value is kept only when it is a number, a duration, a boolean, an absolute path
or a colon list of them; anything else becomes `<value>`. A systemd directive such as `Type=simple` passes, except
Environment=, whose assignments are filtered. Output stops at OUT_MAX bytes.

It runs under /usr/bin/python3, Ubuntu 24.04's 3.12, not uv's pinned interpreter: that one sits
under a user's home, and root never executes a file a user can write. `-I` keeps PYTHONPATH,
the user site and the working directory out of the interpreter.

Exit codes: 0 the verb ran; 1 its command exited non-zero; 2 the request was refused; 3 the
host lacks the command.
"""

from __future__ import annotations

import fnmatch
import os
import re
import shlex
import stat
import subprocess
import sys

# Each verb's whole argv. A path verb's list ends in `--`, and the checked path is the one word
# added after it.
COMMANDS: dict[str, list[str]] = {
    "nft": ["/usr/sbin/nft", "list", "ruleset"],
    "ufw": ["/usr/sbin/ufw", "status", "verbose"],
    "sshd": ["/usr/sbin/sshd", "-T"],
    "crontab": ["/usr/bin/crontab", "-l", "-u", "root"],
}
PATH_COMMANDS: dict[str, list[str]] = {
    "ls": ["/usr/bin/ls", "-la", "--"],
    "stat": ["/usr/bin/stat", "--"],
}
PATH_VERBS = ("ls", "stat", "cat")

# DECIDED: /var/lib/gitops-deploy is not on this list, though the operator's brief named it.
# The gitops_deploy role makes it sys_user-owned, so that account already reads it, and a
# caller who owns a directory can plant a link in it. Every root here is root-owned.
ALLOW = (
    "/etc/crontab",
    "/etc/cron.d",
    "/etc/cron.hourly",
    "/etc/cron.daily",
    "/etc/cron.weekly",
    "/etc/cron.monthly",
    "/etc/netplan",
    "/etc/systemd/system",
    "/etc/ufw",
    "/usr/local/bin",
    "/usr/local/sbin",
)
# Refused even under an ALLOW root. Matched against the path and each of its ancestors, and
# fnmatch's `*` crosses `/`.
DENY = (
    "/etc/shadow*",
    "/etc/gshadow*",
    "/etc/sudoers*",
    "/root/.ssh",
    "/home/*/.ssh",
    "/etc/ssh/*_key",
    "*.env",
    "/etc/rancher/k3s/*",
    "/var/lib/rancher/k3s/server/token",
    "*/node-token",
    "*/cred",
    "*/credentials",
    "*/keys.txt",
    "*.agekey",
    "*/sops/age",
    "/var/lib/kubelet/pods",
)
# Content that withholds the whole output rather than redacting it.
REFUSE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY"
    r"|AGE-SECRET-KEY-"
    r"|ENC\[AES256_GCM,"
    r"|(?i:authorization:)"
    r"|(?i:\bbearer\s+[A-Za-z0-9._~+/-])"
    r"|discord(?:app)?\.com/api/webhooks/"
    r"|://[^/\s:@'\"]+:[^/\s@'\"]+@"
    r"|\bK10[0-9a-f]{16,}::"
    r"|\bgh[pousr]_[A-Za-z0-9]{20,}|\bgithub_pat_"
    r"|\bxox[abprs]-|\bsk-ant-|\bglpat-"
)
PATH_SHAPE = re.compile(r"/[A-Za-z0-9._@+:,=/-]{0,4095}")
READ_MAX = 1024 * 1024
OUT_MAX = 256 * 1024
TIMEOUT_S = 30
ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"}

_ASSIGN = re.compile(
    r"(?<![A-Za-z0-9_])([A-Za-z_][A-Za-z0-9_]*)=(\"[^\"]*\"|'[^']*'|[^\s\"';]*)"
)
_DIRECTIVE = re.compile(r"[A-Z][a-z]+(?:[A-Z][A-Za-z0-9]*)*")
_PLAIN = re.compile(
    r"-?\d+(\.\d+)?"
    r"|\d+(\.\d+)?(us|ms|s|sec|m|min|h|hr|d|w)"
    r"|(?i:yes|no|true|false|on|off)"
    r"|/[A-Za-z0-9._/+-]*(:/[A-Za-z0-9._/+-]*)*"
)


class Refused(Exception):
    pass


def config(
    environ: dict[str, str], euid: int | None = None
) -> tuple[tuple[str, ...], str, frozenset[int]]:
    """The ALLOW roots, the command directory override and the uids an ancestor may have."""
    # DECIDED: a test may replace the roots, the binaries and the owner uid, and only when this
    # is not root. Under sudo the euid is 0 and the environment is reset, so a caller can name
    # none of them; as an ordinary user the override reaches nothing the user could not already
    # read. tests/test_rootq.py holds the root case through the euid parameter.
    if (os.geteuid() if euid is None else euid) == 0:
        return ALLOW, "", frozenset({0})
    roots = tuple(p for p in environ.get("HOMELAB_ROOTQ_ALLOW", "").split(":") if p)
    return (
        roots or ALLOW,
        environ.get("HOMELAB_ROOTQ_BIN", ""),
        frozenset({0, int(environ.get("HOMELAB_ROOTQ_OWNER_UID", "0"))}),
    )


def denied(path: str) -> bool:
    """Whether PATH or any of its ancestors matches a DENY pattern. PATH may be unchecked."""
    probe = path
    while True:
        if any(fnmatch.fnmatchcase(probe, pattern) for pattern in DENY):
            return True
        # dirname stops changing at "/" for an absolute path and at "" for a relative one.
        parent = os.path.dirname(probe)
        if parent == probe:
            return False
        probe = parent


def check_path(path: str, roots: tuple[str, ...], owners: frozenset[int]) -> str:
    # The deny list goes first, so a denied path is refused as denied whatever else is wrong
    # with it.
    if denied(path):
        raise Refused(f"{path} is on the deny list")
    if not PATH_SHAPE.fullmatch(path):
        raise Refused(
            "PATH must be absolute and use only letters, digits and . _ @ + : , = / -"
        )
    try:
        resolved = os.path.realpath(path, strict=True)
    except OSError:
        raise Refused(f"{path} does not exist") from None
    if resolved != path:
        raise Refused(
            f"{path} resolves to {resolved}: no symlink, ., .., // or trailing /"
        )
    if not any(
        path == root or path.startswith(root.rstrip("/") + "/") for root in roots
    ):
        raise Refused(f"{path} is under none of: {' '.join(roots)}")
    parent = os.path.dirname(path)
    while True:
        info = os.lstat(parent)
        if info.st_uid not in owners or info.st_mode & 0o022:
            raise Refused(f"{parent} is not owned and writable by root alone")
        if parent == "/":
            return path
        parent = os.path.dirname(parent)


def read_file(path: str) -> str:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as err:
        raise Refused(f"cannot open {path}: {err.strerror}") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise Refused(f"{path} is not a regular file")
        if info.st_nlink != 1:
            raise Refused(f"{path} has {info.st_nlink} hard links")
        if info.st_size > READ_MAX:
            raise Refused(f"{path} is over {READ_MAX} bytes")
        data = os.read(fd, READ_MAX + 1)
    finally:
        os.close(fd)
    if b"\0" in data:
        raise Refused(f"{path} is binary")
    return data.decode("utf-8", "replace")


def _keep(value: str) -> bool:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return bool(_PLAIN.fullmatch(value))


def _assign(match: re.Match) -> str:
    name, value = match.group(1), match.group(2)
    return match.group(0) if _keep(value) else f"{name}=<value>"


def redact_line(line: str) -> str:
    head, sep, rest = line.partition("=")
    if sep and _DIRECTIVE.fullmatch(head.strip()):
        if head.strip() == "Environment":
            try:
                split = shlex.split(rest, posix=True)
            except ValueError:  # An unbalanced quote: keep nothing of the value.
                return f"{head}=<value>"
            words = [
                w
                if "=" not in w or _keep(w.split("=", 1)[1])
                else w.split("=", 1)[0] + "=<value>"
                for w in split
            ]
            return f"{head}={shlex.join(words)}"
        return head + sep + _ASSIGN.sub(_assign, rest)
    return _ASSIGN.sub(_assign, line)


def filter_output(text: str) -> str:
    if REFUSE.search(text):
        raise Refused(
            "the output matches a credential pattern, so none of it is printed"
        )
    out = "\n".join(redact_line(line) for line in text.split("\n"))
    if len(out.encode()) > OUT_MAX:
        out = (
            out.encode()[:OUT_MAX].decode("utf-8", "ignore")
            + f"\n[truncated at {OUT_MAX} bytes]\n"
        )
    return out


def argv_for(verb: str, args: list[str], environ: dict[str, str]) -> list[str] | str:
    """The argv to execute, or for `cat` the path to read. Raises Refused."""
    roots, bindir, owners = config(environ)
    if verb in COMMANDS:
        if args:
            raise Refused(f"{verb} takes no arguments")
        argv = list(COMMANDS[verb])
    elif verb in PATH_VERBS:
        if len(args) != 1:
            raise Refused(f"{verb} takes exactly one PATH")
        path = check_path(args[0], roots, owners)
        if verb == "cat":
            return path
        argv = [*PATH_COMMANDS[verb], path]
    else:
        raise Refused(
            f"unknown verb {verb!r}; one of {' '.join([*COMMANDS, *PATH_VERBS])}"
        )
    if bindir:
        argv[0] = os.path.join(bindir, os.path.basename(argv[0]))
    return argv


def main(argv: list[str], environ: dict[str, str]) -> int:
    if argv in (["-h"], ["--help"]):
        print(__doc__.split("\n\n", 1)[1].rstrip())
        return 0
    try:
        if not argv:
            raise Refused("usage: rootq VERB [PATH]; rootq --help lists the verbs")
        target = argv_for(argv[0], argv[1:], environ)
        if isinstance(target, str):
            sys.stdout.write(filter_output(read_file(target)))
            return 0
        if not os.access(target[0], os.X_OK):
            print(f"missing command: {target[0]}", file=sys.stderr)
            return 3
        result = subprocess.run(
            target,
            env=ENV,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=TIMEOUT_S,
            check=False,
        )
        sys.stdout.write(filter_output(result.stdout))
        sys.stderr.write(filter_output(result.stderr))
    except Refused as err:
        print(f"refused: {err}", file=sys.stderr)
        return 2
    except subprocess.TimeoutExpired:
        print(f"{target[0]} ran past {TIMEOUT_S}s", file=sys.stderr)
        return 1
    if result.returncode:
        print(f"{target[0]} exited {result.returncode}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    os.umask(0o077)
    sys.exit(main(sys.argv[1:], dict(os.environ)))
