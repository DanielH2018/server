#!/usr/bin/env python3
"""Place issue-fanout batches on the session host with the most memory headroom.

Reads user.slice memory.current against its MemoryHigh on daniel-box and daniel-server, picks
the host with the most headroom per batch, creates a fresh worktree there, and starts a headless
Opus agent as a transient user service. daniel-box agents land their PR; daniel-server agents
stop at `gh pr create`. Spec: docs/superpowers/specs/2026-09-06-claude-fanout-placement-design.md

Usage::

    fanout_place.py read
    fanout_place.py launch --batch 1345,1386 [--batch 1288] [--host daniel-box] --orchestrator-branch <b>
    fanout_place.py status <run-id>
    fanout_place.py stop <run-id> [batch]

Exit codes: 0 ok · 1 usage or launch failure · 3 no headroom on any host · 4 no host readable.
"""

import argparse
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fanout_lib import launch as launch_mod
from fanout_lib import manifest as manifest_mod
from fanout_lib import status as status_mod
from fanout_lib.brief import render_brief
from fanout_lib.placement import NoHeadroom, place
from fanout_lib.transport import HOSTS, REPO, Tools, read_host

BATCH_RE = re.compile(r"^\d+(,\d+)*$")
# The SessionStart hook reads `payload["source"]` from stdin, so it needs a JSON payload
# rather than `</dev/null`; --no-python-downloads/--python match the version session-health.sh
# itself pins so this reading is taken by the same interpreter a real session would use.
HEALTH_CMD = (
    f'cd {REPO} && printf \'{{"source":"startup"}}\' | '
    "uv run --no-project --no-python-downloads --python 3.14.6 .claude/hooks/session-health.py"
)


def _readings(tools: Tools, hosts):
    good, bad = [], []
    for host in hosts:
        r = read_host(tools, host)
        (bad if isinstance(r, str) else good).append(r)
    return good, bad


def cmd_read(args, tools: Tools) -> int:
    good, bad = _readings(tools, HOSTS)
    for r in good:
        print(
            f"{r.host}: cap={r.cap_bytes} current={r.current_bytes} agents={r.live_agents}"
        )
    for msg in bad:
        print(msg, file=sys.stderr)
    return 0 if good else 4


def cmd_launch(args, tools: Tools) -> int:
    batches = {}
    for spec in args.batch:
        if not BATCH_RE.match(spec):
            print(
                f"launch: --batch takes issue numbers joined by commas, got {spec!r}",
                file=sys.stderr,
            )
            return 1
        batches[spec.replace(",", "-")] = [int(n) for n in spec.split(",")]
    hosts = [args.host] if args.host else list(HOSTS)
    good, bad = _readings(tools, hosts)
    for msg in bad:
        print(f"placing without {msg}", file=sys.stderr)
    if not good:
        return 4
    try:
        placed = place(list(batches), good, pin=args.host)
    except NoHeadroom as exc:
        print(str(exc), file=sys.stderr)
        return 3
    health = [ln for host in hosts for ln in _health_lines(tools, host)]
    run = manifest_mod.Manifest(
        manifest_mod.new_run_id(datetime.now(UTC)), args.orchestrator_branch, []
    )
    for batch, host in placed:
        issues = [tools.gh_issue(n) for n in batches[batch]]
        brief = render_brief(issues, host, batch, args.orchestrator_branch, health)
        try:
            run.batches.append(
                launch_mod.launch(tools, host, batch, brief, batches[batch])
            )
        except launch_mod.LaunchError as exc:
            print(f"{batch} on {host}: {exc}", file=sys.stderr)
            manifest_mod.save(run, root=args.manifest_root)
            return 1
        print(f"{batch} -> {host} ({launch_mod.unit_name(batch)})")
    path = manifest_mod.save(run, root=args.manifest_root)
    print(f"run {run.run_id} recorded at {path}")
    return 0


def _health_lines(tools: Tools, host: str) -> list[str]:
    # A failed or timed-out read must not read as "clean" in the launched brief: it's the
    # signal the SessionStart banner exists to surface.
    try:
        proc = tools.run(host, HEALTH_CMD, 40.0, None)
    except subprocess.TimeoutExpired:
        return [
            f"[{host}] session-health read failed (timed out) — banner state unknown"
        ]
    if proc.returncode != 0:
        return [
            f"[{host}] session-health read failed (exit {proc.returncode}) — banner state unknown"
        ]
    return [f"[{host}] {ln}" for ln in proc.stdout.splitlines() if ln.strip()]


def cmd_status(args, tools: Tools) -> int:
    run = manifest_mod.load(args.run_id, root=args.manifest_root)
    worst = 0
    for host in sorted({b.host for b in run.batches}):
        mine = [b for b in run.batches if b.host == host]
        try:
            proc = tools.run(
                host, status_mod.status_command(mine), status_mod.STATUS_TIMEOUT_S, None
            )
        except subprocess.TimeoutExpired:
            for b in mine:
                print(f"{b.batch} on {host}: status read timed out")
            worst = 1
            continue
        for s in status_mod.parse_status(mine, proc.stdout):
            line = f"{s.batch} on {host}: {s.state}"
            if s.pr_url:
                line += f" {s.pr_url}"
            if s.state == "failed":
                exit_text = "unknown" if s.exit_code is None else str(s.exit_code)
                line += f" (exit {exit_text}) {s.stderr_tail[-300:]}"
                worst = 1
            print(line)
    return worst


def cmd_stop(args, tools: Tools) -> int:
    run = manifest_mod.load(args.run_id, root=args.manifest_root)
    for b in run.batches:
        if args.batch and b.batch != args.batch:
            continue
        try:
            proc = tools.run(b.host, status_mod.stop_command(b.unit), 30.0, None)
        except subprocess.TimeoutExpired:
            print(f"{b.batch} on {b.host}: stop timed out")
            continue
        print(
            f"{b.batch} on {b.host}: {'stopped' if proc.returncode == 0 else proc.stderr.strip()}"
        )
    return 0


def _add_manifest_root(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--manifest-root",
        type=Path,
        default=manifest_mod.MANIFEST_DIR,
        help=argparse.SUPPRESS,
    )


def main(argv=None, tools: Tools | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0], allow_abbrev=False)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("read").set_defaults(fn=cmd_read)
    launch_parser = sub.add_parser("launch")
    launch_parser.add_argument(
        "--batch",
        action="append",
        required=True,
        help="issue numbers joined by commas; repeatable",
    )
    launch_parser.add_argument("--host", choices=HOSTS)
    launch_parser.add_argument(
        "--orchestrator-branch", required=True, help="the branch holding the claims"
    )
    _add_manifest_root(launch_parser)
    launch_parser.set_defaults(fn=cmd_launch)
    status_parser = sub.add_parser("status")
    status_parser.add_argument("run_id")
    _add_manifest_root(status_parser)
    status_parser.set_defaults(fn=cmd_status)
    stop_parser = sub.add_parser("stop")
    stop_parser.add_argument("run_id")
    stop_parser.add_argument("batch", nargs="?")
    _add_manifest_root(stop_parser)
    stop_parser.set_defaults(fn=cmd_stop)
    args = p.parse_args(argv)
    return args.fn(args, tools or Tools())


if __name__ == "__main__":
    sys.exit(main())
