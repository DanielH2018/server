#!/usr/bin/env python3
"""The `deploy` source for cc-wait: one `deploy.sh --detach` run's state, read from its log.

`cc-wait deploy <tags>` runs this through `.claude/wait-sources/deploy`. cc-wait owns the loop,
the budget and the output; this answers one question per call: where is this deploy now?

    deploy_probe.py --describe <tags> [--log PATH | --log-dir DIR]   # terminal states, as JSON
    deploy_probe.py <tags> [--log PATH | --log-dir DIR]              # the state now, one JSON line

The contract is the dotfiles repo's docs/specs/2026-10-04-cc-wait-design.md.

WHICH DEPLOY. `--log` names it exactly, and `deploy.sh --detach` prints the command with it.
Without `--log`, `<tags>` is the comma-separated `--tags` value the deploy was given, and the
newest `deploy-<label>-<stamp>-<pid>.log` in `--log-dir` (default: `deploy_detach.LOG_DIR`) is
the deploy. The match is exact on the label, so `sonarr` never picks up a `sonarr-exporter`
log. `deploy.sh --detach` creates the log before it returns, so a wait chained after it never
reads an earlier deploy of the same tags. A deploy of so many tags that `Run.label` cut the
label short needs `--log`.

THE EXIT CODE IS THE AUTHORITY. The detached child records the notifier's exit code beside the
log (`deploy_detach.child`): 0 when the health gate settled, 1 when the playbook or the gate
failed. The notifier's `deploy --detach settled|FAILED (...)` headline is the detail. A child
that died without recording a code is `died`, through the pid the parent recorded.
"""

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # scripts/
from lib.detach_fork import alive, recorded_code, recorded_pid

# `deploy_detach.LOG_DIR`, restated: that module imports the deploy machinery, and this probe
# runs under a bare interpreter with no project environment. A test holds the two equal.
LOG_DIR = Path("/tmp/homelab-deploy-logs")

# Each terminal state -> the exit code cc-wait ends with.
TERMINAL = {"settled": 0, "failed": 1, "died": 1}

_HEADLINE = re.compile(r"^deploy --detach (?:settled|FAILED) .*$", re.MULTILINE)
_TASK = re.compile(r"^(?:TASK|PLAY) \[(.+?)\]", re.MULTILINE)


class NoDeploy(Exception):
    """No log exists for the deploy the arguments name."""


def label(tags: str) -> str:
    """The log-name label `Run.label` gives these comma-separated tags (uncapped)."""
    joined = "+".join(t for t in tags.split(",") if t)
    return re.sub(r"[^A-Za-z0-9_.-]", "_", joined or "full")


def find_log(tags: str, log: str, log_dir: str) -> Path:
    """The log of the deploy the arguments name.

    Raises:
      NoDeploy: `--log` names a missing file, or `--log-dir` holds no log for `tags`.
    """
    if log:
        path = Path(log)
        if not path.exists():
            raise NoDeploy(f"{path} does not exist")
        return path
    base = Path(log_dir) if log_dir else LOG_DIR
    name = re.compile(
        rf"^deploy-{re.escape(label(tags))}-(\d{{8}}-\d{{6}})-(\d+)\.log$"
    )
    # (stamp, pid, path): the pid breaks a same-second tie, numerically.
    runs = [
        (m.group(1), int(m.group(2)), p)
        for p in base.glob("deploy-*.log")
        if (m := name.match(p.name))
    ]
    if not runs:
        raise NoDeploy(f"no deploy-{label(tags)}-*.log in {base}")
    return max(runs)[2]


def describe(log: Path) -> dict:
    return {"terminal": TERMINAL, "watch": [str(log.parent)], "interval_s": 10}


def _verdict_detail(text: str, log: Path) -> str:
    headline = _HEADLINE.findall(text)
    if headline:
        return headline[-1]
    # A child that crashed (`traceback.print_exc`) ends on the exception's own line: the last
    # unindented line after the last `Traceback`.
    _, traceback, after = text.rpartition("Traceback (most recent call last):")
    if traceback:
        crash = [line for line in after.splitlines() if line and not line[0].isspace()]
        if crash:
            return f"{crash[-1]}; read {log}"
    return f"no verdict line; read {log}"


def read(log: Path) -> dict:
    """The deploy's state: terminal once it recorded a code, or once it died without one."""
    code = recorded_code(log)
    if code is None:
        pid = recorded_pid(log)
        # The child writes its code and then exits, so the code is read again after a pid that
        # is gone: only a child killed mid-run leaves neither.
        if pid is not None and not alive(pid):
            code = recorded_code(log)
            if code is None:
                return {
                    "state": "died",
                    "detail": f"the deploy (pid {pid}) died without recording an exit code; "
                    f"read {log}",
                }
    text = log.read_text(errors="replace")
    if code is not None:
        detail = _verdict_detail(text, log)
        if code == 0:
            return {"state": "settled", "detail": detail}
        return {"state": "failed", "detail": f"exit code {code}: {detail}"}
    tasks = _TASK.findall(text)
    return {"state": "running", "detail": tasks[-1] if tasks else str(log)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="deploy_probe.py", description=__doc__.splitlines()[0]
    )
    parser.add_argument("--describe", action="store_true")
    parser.add_argument("tags", help="the --tags value the deploy was given")
    where = parser.add_mutually_exclusive_group()
    where.add_argument("--log", default="")
    where.add_argument("--log-dir", default="")
    ns = parser.parse_args(argv)
    try:
        log = find_log(ns.tags, ns.log, ns.log_dir)
    except NoDeploy as exc:
        print(f"deploy_probe: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(describe(log) if ns.describe else read(log)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
