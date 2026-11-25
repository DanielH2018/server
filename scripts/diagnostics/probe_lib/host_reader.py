"""One read of a host's journal, files and unit state, made in a single process on that host.

`probe.py builder`, `lander` and `unit` each read state that lives on one host: a system unit's
journal, a textfile, a landing log, a timer's next trigger.
Every such read goes through `read_host`, which runs `READER` there with `python3 -I -` and
parses the one JSON document it prints. On the host itself that is a local process; anywhere
else it is ONE ssh call, because `ufw limit ssh` refuses a sixth connection in 30 seconds. A
read of daniel-pi from a host other than daniel-box makes that call through daniel-box.

`READER` is stdlib-only. `read_host` writes the plan into it as a string literal, because
`python3 -` reads the whole of stdin as the program. A plan is a JSON object with any of five
keys:

- `journal`: `{key: [unit, since, until]}`, or `[unit, since, until, by, lines]`. Each read is
  `journalctl -u <unit> -o json`. A `by` of `-t` reads a syslog identifier instead, and
  `match` passes the first element as a `FIELD=value` match (`UNIT=<unit>` is systemd's own
  lines about the unit). `lines`
  keeps only the newest that many, and a null `since` reads without a window. Its entries
  come back as `[epoch_s, priority, invocation_id, identifier, message]`.
- `files`: paths whose text to return. A leading `~` is the reading user's home on that host.
- `show`: `{unit: [property, ...]}` read with `systemctl show`.
- `timers`: timer units whose `systemctl list-timers -o json` row to return, null for one
  systemd does not list. Its `next` is wall-clock microseconds even for a monotonic timer,
  whose `NextElapseUSecRealtime` property is empty.
- `logs`: `[directory, glob, since_epoch]`. Each match newer than the cutoff returns its
  `VERDICT:` line, its `.rc` text and whether the pid in its `.pid` is alive.

ABSENT IS NOT UNREADABLE. A file or a log directory reports `absent`,
`unreadable` or `ok`, never an empty value for a read that failed. A journal read by a user
outside `adm` and `systemd-journal` exits 0 with no entries and a hint on stderr, which would
read as "the unit never ran"; `journal_entries` raises `core.Unreadable` on that hint.
"""

import json
import re
import shlex
import socket
import subprocess
from dataclasses import dataclass

from diagnostics.probe_lib import core
from lib.inventory_lists import LANDING_HOST, PI_HOST
from lib.systemd_timer import span_s

BOX = "daniel-box"
READ_TIMEOUT_S = 90

READER = r"""
import fnmatch, json, os, pwd, subprocess
plan = json.loads(__PLAN__)
out = {"host": os.uname().nodename, "user": pwd.getpwuid(os.getuid()).pw_name}

def journal(unit, since, until, by="-u", lines=None):
    argv = ["journalctl", *([] if by == "match" else [by]), unit, "-o", "json", "--no-pager",
            "--output-fields=MESSAGE,PRIORITY,_SYSTEMD_INVOCATION_ID,SYSLOG_IDENTIFIER"]
    if since:
        argv += ["--since", since]
    if until:
        argv += ["--until", until]
    if lines:
        argv += ["-n", str(lines)]
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
        with open(os.path.expanduser(path), errors="replace") as f:
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

def show(unit, props):
    argv = ["systemctl", "show", unit] + ["-p" + p for p in props]
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    return dict(line.split("=", 1) for line in p.stdout.splitlines() if "=" in line)

def timer(unit):
    argv = ["systemctl", "list-timers", unit, "--all", "-o", "json"]
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=20)
        rows = json.loads(p.stdout or "[]")
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    return next((r for r in rows if r.get("unit") == unit), None)

out["journal"] = {k: journal(*v) for k, v in plan.get("journal", {}).items()}
out["timers"] = {u: timer(u) for u in plan.get("timers", [])}
out["files"] = {p: text(p) for p in plan.get("files", [])}
out["show"] = {u: show(u, ps) for u, ps in plan.get("show", {}).items()}
out["logs"] = logs(*plan["logs"]) if plan.get("logs") else None
print(json.dumps(out))
"""

# journalctl prints one of these to stderr and exits 0 with nothing for a user who may not read the
# system journal.
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
    """The command that runs `READER` on `host`, seen from the host named `here`.

    Only `LANDING_HOST` holds a key daniel-pi accepts, so a read of the Pi from anywhere else
    hops through it, as `core.pi_ssh_argv` does.
    """
    here = here.split(".")[0]
    if here == host:
        return ["python3", "-I", "-"]
    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
    if host == PI_HOST and here != LANDING_HOST:
        return [*ssh, LANDING_HOST, shlex.join([*ssh, host, "python3 -I -"])]
    return [*ssh, host, "python3 -I -"]


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
