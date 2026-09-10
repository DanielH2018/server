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

Exit codes: 0 ok · 1 usage or launch failure · 3 no headroom on any host, or a placement
puts more than MAX_BATCHES_PER_REMOTE_HOST batches on one remote host · 4 no host readable
· 5 a batch reports failed (`status` only) · 6 no candidate host signs commits GitHub
verifies, or the account's registered signing keys could not be read.

A launch costs two remote ssh connections per host it reads (headroom, health) plus one
per batch placed there — worktree add+lock, brief write and systemd-run folded into one
call. `--host` pins every batch to one host; leave it unset and placement reads both hosts
and chooses per batch. Calls to the host this script itself runs on go over `bash -c`, not
ssh, so they never count against `ufw limit ssh` — see MAX_BATCHES_PER_REMOTE_HOST below for
the cap that keeps a placement on an actual remote host under it.

Launch locks each worktree and nothing in this slice unlocks it. Until `clean` lands, release
a stopped or failed batch's tree by hand::

    git -C /home/ubuntu/server worktree unlock <worktree>
    uv run python scripts/dev/prune_worktrees.py --prune
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
from fanout_lib import signing as signing_mod
from fanout_lib import status as status_mod
from fanout_lib.brief import REQUIRED_LABEL, Issue, render_brief
from fanout_lib.placement import HostReading, NoHeadroom, place
from fanout_lib.transport import HOSTS, REPO, Tools, read_host


def _registered_keys(tools: Tools) -> frozenset[str] | str:
    """The account's registered signing keys, or a one-line reason they could not be read."""
    try:
        return tools.signing_keys()
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        ValueError,
    ) as exc:
        return (
            "could not read the GitHub account's registered signing keys "
            f"({_error_text(exc)})"
        )


def _signing_verified(
    readings: list[HostReading], registered: frozenset[str]
) -> list[HostReading]:
    """Drop every host GitHub would not verify, saying which and why.

    A dropped host is not a failure to retry: its commits would read `verified=false
    reason=unknown_key`, and the PR the agent opens there cannot merge past the
    verified-signatures rule until someone re-signs the branch by hand (#1615).
    """
    keep = []
    for r in readings:
        reason = signing_mod.unverified_reason(r.host, r.signing_key, registered)
        if reason is None:
            keep.append(r)
        else:
            print(f"launch: not placing on {reason}", file=sys.stderr)
    return keep


BATCH_RE = re.compile(r"^\d+(,\d+)*$")
# The host this script normally runs on; its launches go over `bash -c`, never ssh, so the
# per-host ssh budget below doesn't apply to it.
LOCAL_HOST = "daniel-box"
# ufw limit ssh REJECTs a 6th connection to one host within 30s. Two of those five go to the
# headroom and health reads, leaving room for at most this many batch launches — each its
# own ssh connection — before the run risks the 6th.
MAX_BATCHES_PER_REMOTE_HOST = 3
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
    # `signing=` makes the launch gate's verdict readable before a launch spends an agent on
    # it. `unknown` means the registered-key read itself failed, which `launch` refuses on.
    registered = _registered_keys(tools)
    for r in good:
        if isinstance(registered, str):
            verdict = "unknown"
        else:
            verdict = (
                "unverified"
                if signing_mod.unverified_reason(r.host, r.signing_key, registered)
                else "ok"
            )
        print(
            f"{r.host}: cap={r.cap_bytes} current={r.current_bytes} "
            f"agents={r.live_agents} signing={verdict}"
        )
    if isinstance(registered, str):
        print(registered, file=sys.stderr)
    for msg in bad:
        print(msg, file=sys.stderr)
    return 0 if good else 4


def _parse_batches(specs: list[str]) -> dict[str, list[int]] | None:
    """Map each `--batch` spec to its issue numbers, or None when a spec is unusable.

    An identical spec given twice is placed once and reported; the same issue number in two
    different specs is refused — launching it twice means two agents in two worktrees on one
    issue, which the claim in the brief cannot undo.
    """
    batches: dict[str, list[int]] = {}
    seen: dict[int, str] = {}
    for spec in specs:
        if not BATCH_RE.match(spec):
            print(
                f"launch: --batch takes issue numbers joined by commas, got {spec!r}",
                file=sys.stderr,
            )
            return None
        key = spec.replace(",", "-")
        if key in batches:
            print(
                f"launch: --batch {spec} given twice; placing it once", file=sys.stderr
            )
            continue
        numbers = [int(n) for n in spec.split(",")]
        for n in numbers:
            if n in seen and seen[n] == spec:
                print(
                    f"launch: issue {n} is listed twice in --batch {spec}",
                    file=sys.stderr,
                )
                return None
            if n in seen:
                print(
                    f"launch: issue {n} appears in more than one --batch "
                    f"({seen[n]}, {spec}); refusing to launch it twice",
                    file=sys.stderr,
                )
                return None
            seen[n] = spec
        batches[key] = numbers
    return batches


def _error_text(exc: BaseException) -> str:
    # CalledProcessError.stderr is text under `text=True`; TimeoutExpired.stderr is bytes
    # or None. Printing either straight would raise inside the handler that exists to make
    # the failure clean.
    err = getattr(exc, "stderr", None)
    if isinstance(err, bytes):
        err = err.decode("utf-8", "replace")
    return (err or str(exc)).strip()


def _fetch_issues(
    tools: Tools, batches: dict[str, list[int]]
) -> dict[int, Issue] | None:
    """Fetch every batch's issues up front, or return None having said why it refused.

    Every fetch completes before the first launch on purpose: a fetch that failed inside the
    placement loop left the batches already launched running under `auto`, in worktrees this
    slice locks, with no manifest naming them.
    """
    fetched: dict[int, Issue] = {}
    for number in [n for numbers in batches.values() for n in numbers]:
        try:
            fetched[number] = tools.gh_issue(number)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            print(
                f"launch: could not fetch issue {number}: {_error_text(exc)}",
                file=sys.stderr,
            )
            return None
    unlabelled = [
        n for n, issue in fetched.items() if REQUIRED_LABEL not in issue.labels
    ]
    for number in unlabelled:
        print(
            f"launch: issue {number} does not carry the `{REQUIRED_LABEL}` label — only "
            "issues findings.py filed are fan-out work",
            file=sys.stderr,
        )
    return None if unlabelled else fetched


def _over_ssh_budget(placed: list[tuple[str, str]]) -> bool:
    """Print and return True when the placement puts too many batches on one remote host."""
    counts: dict[str, int] = {}
    for _, host in placed:
        counts[host] = counts.get(host, 0) + 1
    over = False
    for host, n in counts.items():
        if host != LOCAL_HOST and n > MAX_BATCHES_PER_REMOTE_HOST:
            print(
                f"launch: {n} batches would land on {host}, more than "
                f"{MAX_BATCHES_PER_REMOTE_HOST} fits the ssh budget there — split the "
                "fan-out",
                file=sys.stderr,
            )
            over = True
    return over


def cmd_launch(args, tools: Tools) -> int:
    batches = _parse_batches(args.batch)
    if batches is None:
        return 1
    fetched = _fetch_issues(tools, batches)
    if fetched is None:
        return 1
    # Read the registered keys before the first ssh: a gh outage then refuses having spent no
    # connection against the per-host ssh limit.
    registered = _registered_keys(tools)
    if isinstance(registered, str):
        print(f"launch: {registered} — refusing to launch", file=sys.stderr)
        return 6
    hosts = [args.host] if args.host else list(HOSTS)
    good, bad = _readings(tools, hosts)
    for msg in bad:
        print(f"placing without {msg}", file=sys.stderr)
    if not good:
        return 4
    good = _signing_verified(good, registered)
    if not good:
        print(
            "launch: no candidate host signs commits GitHub verifies — a batch placed there "
            "opens a PR that cannot merge",
            file=sys.stderr,
        )
        return 6
    try:
        placed = place(list(batches), good, pin=args.host)
    except NoHeadroom as exc:
        print(str(exc), file=sys.stderr)
        return 3
    if _over_ssh_budget(placed):
        return 3
    health = [ln for host in hosts for ln in _health_lines(tools, host)]
    run = manifest_mod.Manifest(
        manifest_mod.new_run_id(datetime.now(UTC)), args.orchestrator_branch, []
    )
    for batch, host in placed:
        issues = [fetched[n] for n in batches[batch]]
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


def _one_line(text: str, limit: int = 300) -> str:
    # Collapse the whitespace before truncating: a cut that lands mid-line would put a
    # newline inside a status line that is meant to be one line per batch.
    return " ".join(text.split())[:limit]


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
            worst = max(worst, 1)
            continue
        for s in status_mod.parse_status(mine, proc.stdout):
            line = f"{s.batch} on {host}: {s.state}"
            if s.pr_url:
                line += f" {s.pr_url}"
            if s.permission_denials:
                line += f" permission_denials={s.permission_denials}"
            if s.state == "done":
                line += f" {_one_line(s.final_text)}"
            if s.state == "failed":
                exit_text = "unknown" if s.exit_code is None else str(s.exit_code)
                line += f" (exit {exit_text})"
                if s.terminal_reason:
                    line += f" {s.terminal_reason}"
                line += f" {s.stderr_tail[-300:]}"
                worst = max(worst, 5)
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
        # Nothing in this slice unlocks the tree, so the operator has to. Printing the
        # command beside the stop is the only place it meets someone who needs it.
        print(
            f"  release its worktree: git -C {REPO} worktree unlock {b.worktree} "
            "&& uv run python scripts/dev/prune_worktrees.py --prune"
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
    launch_parser.add_argument(
        "--host",
        choices=HOSTS,
        help="pin every batch to this host; omit it to let placement choose per batch",
    )
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
