"""One read of a host's journal, files and unit state, made in a single process on that host.

`probe.py builder`, `lander`, `gitops-state --why` and `applied` each read state that lives on
one host: a system unit's journal, a textfile, a landing log, an installed file's mode and hash.
Every such read goes through `read_host`, which runs `READER` there with `python3 -I -` and
parses the one JSON document it prints. On the host itself that is a local process; anywhere
else it is ONE ssh call, because `ufw limit ssh` refuses a sixth connection in 30 seconds.

`READER` is stdlib-only. `read_host` writes the plan into it as a string literal, because
`python3 -` reads the whole of stdin as the program. A plan is a JSON object with any of five
keys:

- `journal`: `{key: [unit, since, until]}`. Each read is `journalctl -u <unit> -o json`, and its
  entries come back as `[epoch_s, priority, invocation_id, identifier, message]`.
- `files`: paths whose text to return.
- `show`: `{unit: [property, ...]}` read with `systemctl show`.
- `logs`: `[directory, glob, since_epoch]`. Each match newer than the cutoff returns its
  `VERDICT:` line, its `.rc` text and whether the pid in its `.pid` is alive.
- `stat`: paths whose existence, mode, owner, group and sha256 to return.

ABSENT IS NOT UNREADABLE. A file, a log directory or a stat target reports `absent`,
`unreadable` or `ok`, never an empty value for a read that failed. A journal read by a user
outside `adm` and `systemd-journal` exits 0 with no entries and a hint on stderr, which would
read as "the unit never ran"; `journal_entries` raises `core.Unreadable` on that hint.
"""

# `probe_lib` is a namespace package under `scripts/`, so reaching a sibling by package name
# needs `scripts/` on sys.path — a module gets only its importer's path otherwise, and
# pyproject's `pythonpath` is a pytest setting. This has to sit ABOVE the imports below.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import json
import re
import socket
import subprocess
from dataclasses import dataclass

from diagnostics.probe_lib import core
from lib.systemd_timer import span_s

BOX = "daniel-box"
READ_TIMEOUT_S = 90

READER = r"""
import fnmatch, grp, hashlib, json, os, pwd, subprocess
plan = json.loads(__PLAN__)
out = {"host": os.uname().nodename, "user": pwd.getpwuid(os.getuid()).pw_name}

def _name(lookup, ident):
    try:
        return lookup(ident)[0]
    except KeyError:
        return str(ident)

def journal(unit, since, until):
    argv = ["journalctl", "-u", unit, "--since", since, "-o", "json", "--no-pager",
            "--output-fields=MESSAGE,PRIORITY,_SYSTEMD_INVOCATION_ID,SYSLOG_IDENTIFIER"]
    if until:
        argv += ["--until", until]
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"rc": -1, "stderr": str(exc), "entries": []}
    entries = []
    for line in p.stdout.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        msg = e.get("MESSAGE") or ""
        if isinstance(msg, list):
            msg = bytes(msg).decode("utf-8", "replace")
        entries.append([int(e.get("__REALTIME_TIMESTAMP", 0)) / 1e6, int(e.get("PRIORITY", 6)),
                        e.get("_SYSTEMD_INVOCATION_ID", ""), e.get("SYSLOG_IDENTIFIER", ""), msg])
    return {"rc": p.returncode, "stderr": p.stderr[-600:], "entries": entries}

def text(path):
    try:
        with open(path, errors="replace") as f:
            return {"status": "ok", "text": f.read()}
    except FileNotFoundError:
        return {"status": "absent"}
    except OSError as exc:
        return {"status": "unreadable", "error": str(exc)}

def alive(pid_text):
    try:
        os.kill(int(pid_text.strip()), 0)
    except PermissionError:
        return True
    except (ValueError, OSError):
        return False
    return True

def logs(directory, pattern, since):
    # listdir, not isdir: isdir is False on EACCES too, which would read a parent this user
    # cannot traverse as an absent directory.
    try:
        names = os.listdir(directory)
    except FileNotFoundError:
        return {"status": "absent"}
    except OSError as exc:
        return {"status": "unreadable", "error": str(exc)}
    rows = []
    for path in sorted(os.path.join(directory, n) for n in fnmatch.filter(names, pattern)):
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            continue
        if mtime < since:
            continue
        base = path[: -len(".log")]
        row = {"name": os.path.basename(path), "mtime": mtime, "verdict": None}
        log = text(path)
        row["status"] = log["status"]
        for line in log.get("text", "").splitlines():
            if line.startswith("VERDICT:"):
                row["verdict"] = line
        row["rc"] = text(base + ".rc").get("text")
        pid = text(base + ".pid").get("text")
        row["alive"] = bool(pid) and alive(pid)
        rows.append(row)
    return {"status": "ok", "rows": rows}

def stat(path):
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return {"status": "absent"}
    except OSError as exc:
        return {"status": "unreadable", "error": str(exc)}
    row = {"status": "ok", "mode": "%04o" % (st.st_mode & 0o7777),
           "owner": _name(pwd.getpwuid, st.st_uid), "group": _name(grp.getgrgid, st.st_gid),
           "sha256": None}
    try:
        with open(path, "rb") as f:
            row["sha256"] = hashlib.sha256(f.read()).hexdigest()
    except OSError as exc:
        row["content"] = str(exc)
    return row

def show(unit, props):
    argv = ["systemctl", "show", unit] + ["-p" + p for p in props]
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    return dict(line.split("=", 1) for line in p.stdout.splitlines() if "=" in line)

out["journal"] = {k: journal(*v) for k, v in plan.get("journal", {}).items()}
out["files"] = {p: text(p) for p in plan.get("files", [])}
out["show"] = {u: show(u, ps) for u, ps in plan.get("show", {}).items()}
out["logs"] = logs(*plan["logs"]) if plan.get("logs") else None
out["stat"] = {p: stat(p) for p in plan.get("stat", [])}
print(json.dumps(out))
"""

# journalctl prints one of these to stderr, and exits 0 with nothing, for a user who may not
# read the system journal.
_NO_JOURNAL_ACCESS = (
    "not seeing messages from other users",
    "insufficient permissions",
)


@dataclass(frozen=True)
class Entry:
    """One journal line: when, its syslog priority, which unit run, who wrote it, the text."""

    at: float
    priority: int
    invocation: str
    identifier: str
    message: str


def unit_span_s(text: str, key: str) -> int:
    """The seconds a systemd unit's `key=` duration names, such as `TimeoutStartSec=20min`.

    Raises:
        ValueError: the unit text has no such line, or its value is not a duration.
    """
    m = re.search(rf"^{key}=(\S+)", text, re.M)
    if m is None:
        raise ValueError(f"no {key}= line")
    return span_s(m.group(1))


def reader_argv(host: str, here: str) -> list[str]:
    """The command that runs `READER` on `host`, seen from the host named `here`."""
    if here.split(".")[0] == host:
        return ["python3", "-I", "-"]
    return [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, "python3 -I -",
    ]  # fmt: skip


def read_host(
    plan: dict,
    host: str = BOX,
    *,
    run=subprocess.run,
    hostname=socket.gethostname,
    timeout: float = READ_TIMEOUT_S,
) -> dict:
    """Run `READER` with `plan` on `host` and return its document.

    Raises:
        core.Unreadable: the process could not start, timed out, exited non-zero or printed
            something other than one JSON object.
    """
    argv = reader_argv(host, hostname())
    stdin = READER.replace("__PLAN__", repr(json.dumps(plan)))
    try:
        done = run(argv, input=stdin, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise core.Unreadable(f"could not read {host}: {exc}") from exc
    if done.returncode != 0:
        tail = " ".join((done.stderr or "").split())[-300:]
        raise core.Unreadable(f"could not read {host}: exit {done.returncode}: {tail}")
    try:
        doc = json.loads(done.stdout)
    except ValueError as exc:
        raise core.Unreadable(f"{host} printed no JSON document: {exc}") from exc
    if not isinstance(doc, dict):
        raise core.Unreadable(f"{host} printed {type(doc).__name__}, not an object")
    return doc


def journal_entries(doc: dict, key: str) -> list[Entry]:
    """The entries of the journal read `key`, oldest first.

    Raises:
        core.Unreadable: journalctl failed, or it hid the system journal from this user.
    """
    read = (doc.get("journal") or {}).get(key)
    if read is None:
        raise core.Unreadable(
            f"the read of {doc.get('host')} returned no {key} journal"
        )
    stderr = read.get("stderr") or ""
    if read.get("rc") != 0 or any(hint in stderr for hint in _NO_JOURNAL_ACCESS):
        tail = " ".join(stderr.split())[-300:]
        raise core.Unreadable(
            f"journalctl for {key} on {doc.get('host')} as {doc.get('user')} read nothing "
            f"(exit {read.get('rc')}): {tail}"
        )
    return sorted(
        (Entry(*row) for row in read.get("entries") or []), key=lambda e: e.at
    )


def dry_run_text(plan: dict, host: str = BOX, here: str | None = None) -> str:
    """What `--dry-run` prints: the command and the plan it would send."""
    argv = reader_argv(host, here or socket.gethostname())
    return (
        f"{' '.join(argv)}   # stdin: host_reader.READER, carrying the plan below\n"
        + (json.dumps(plan, indent=2))
    )
