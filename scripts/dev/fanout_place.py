#!/usr/bin/env python3
"""Place issue-fanout batches on the session host with the most memory headroom.

Reads memory.current against MemoryHigh for BOTH cgroups an agent lives in — user.slice (the
fleet) and user-1000.slice (the login plane) — on daniel-box and daniel-server, scores each host
on the tighter of the two, picks the host with the most headroom per batch, creates a fresh
worktree there, and starts a headless
Opus agent as a transient user service. daniel-box agents land their PR; daniel-server agents
stop at `gh pr create`. Spec: docs/superpowers/specs/2026-09-06-claude-fanout-placement-design.md

Usage::

    fanout_place.py read
    fanout_place.py launch --batch 1345,1386 [--batch 1288] [--host daniel-box] --orchestrator-branch <b>
    fanout_place.py status <run-id>
    fanout_place.py stop <run-id> [batch]
    fanout_place.py clean <run-id>

Exit codes: 0 ok · 1 usage or launch failure · 3 no headroom on any host, or a placement
puts more than MAX_BATCHES_PER_REMOTE_HOST batches on one remote host · 4 no host readable
· 5 a batch reports failed (`status` only).

A launch costs two remote ssh connections per host it reads (headroom, health) plus one
per batch placed there — worktree add+lock, brief write and systemd-run folded into one
call. `--host` pins every batch to one host; leave it unset and placement reads both hosts
and chooses per batch. Calls to the host this script itself runs on go over `bash -c`, not
ssh, so they never count against `ufw limit ssh` — see MAX_BATCHES_PER_REMOTE_HOST below for
the cap that keeps a placement on an actual remote host under it.

Launch locks each worktree with reason `fanout-<batch>` so a merged-worktree prune cannot
remove it while the unit still runs; `clean <run-id>` is the escape — it removes a batch's
worktree, unlocking it in the process, once the batch's PR merged and the tree is clean, and
leaves a batch that isn't ready to go both locked and in the manifest.
"""

import argparse
import dataclasses
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
from fanout_lib.brief import REQUIRED_LABEL, Issue, render_brief
from fanout_lib.placement import NoHeadroom, place
from fanout_lib.transport import HOSTS, REPO, Tools, read_host

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
    for r in good:
        # Both caps, because placement scores the tighter of the two: a fleet number with
        # room says nothing on its own about whether a batch fits.
        print(
            f"{r.host}: fleet cap={r.cap_bytes} current={r.current_bytes} "
            f"plane cap={r.plane_cap_bytes} current={r.plane_current_bytes} "
            f"agents={r.live_agents}"
        )
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
            # The refusal is per batch, after placement, so earlier batches are already
            # running. Name them and the run-id: the manifest is what `status` and `clean`
            # read, and the `clean <run-id>` an `exists` refusal asks for needs the id.
            manifest_mod.save(run, root=args.manifest_root)
            launched = ", ".join(b.batch for b in run.batches) or "none"
            print(
                f"launched before this failure: {launched} (run {run.run_id})",
                file=sys.stderr,
            )
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
    # A cleaned batch is reported from the manifest and never read remotely. `clean` resets
    # the failed unit and takes .fanout/report.json with the worktree, so status_command
    # finds no active state, no result and no report — and `parse_status` reads exactly that
    # as `failed`, which would exit 5 for a batch that landed its PR and was tidied up.
    for b in run.batches:
        if b.removed_at:
            print(f"{b.batch} on {b.host}: cleaned ({b.removed_at})")
    live = [b for b in run.batches if not b.removed_at]
    for host in sorted({b.host for b in live}):
        mine = [b for b in live if b.host == host]
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
        # The worktree stays locked until `clean` runs it through prune_worktrees' content
        # check — stopping a batch says nothing about whether its PR merged.
        print(f"  run `clean {args.run_id}` once its PR merges")
    return 0


def cmd_clean_one(
    args,
    tools: Tools,
    list_worktrees=None,
    ask=None,
    dirty=None,
    remover=None,
    unlocker=None,
    locker=None,
) -> int:
    """Hidden: runs ON the host holding the worktree. `clean` calls it over Tools.run.

    `list_worktrees`, `ask`, `dirty`, `remover`, `unlocker` and `locker` are seams —
    parameters rather than patched module attributes, so a test can drive this without
    pinning a first-party module name. Every default reaches the real thing, and all five
    of `clean_one`'s own seams are forwarded so a REMOVABLE-with-lock case run through this
    entry point stays hermetic too.

    An absent worktree reads `removed`, not `kept` — it is the goal state, not a failure.
    This is no longer how `clean` handles a gone tree: the copy of this script that a
    remote leg runs lives inside the worktree, so `remote_clean_command`'s shell chain
    answers the absent-tree case before this interpreter could start (Ruling 30). The path
    below stays for a `clean-one` run by hand against a tree that is already gone.
    """
    from fanout_lib.clean import clean_one
    from fanout_lib.clean import lock as default_locker
    from fanout_lib.clean import unlock as default_unlocker
    from prune_worktrees import is_dirty, is_merged, parse_worktree_list, remove

    if list_worktrees is None:

        def list_worktrees():
            return subprocess.run(
                ["git", "-C", REPO, "worktree", "list", "--porcelain"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout

    # Resolved paths, not exact string equality: a worktree launched through one spelling
    # of REPO (a symlink, say) is still the tree `git worktree list` names by its target.
    target = Path(args.worktree).resolve()
    tree = next(
        (
            t
            for t in parse_worktree_list(list_worktrees())
            if Path(t.path).resolve() == target
        ),
        None,
    )
    if tree is None:
        print(f"removed: {args.worktree} (already gone)")
        return 0
    state, why = clean_one(
        REPO,
        tree,
        ask=ask if ask is not None else is_merged,
        dirty=dirty if dirty is not None else is_dirty,
        remover=remover if remover is not None else remove,
        unlocker=unlocker if unlocker is not None else default_unlocker,
        locker=locker if locker is not None else default_locker,
    )
    print(f"{state}: {args.worktree} {why}".rstrip())
    return 0


def cmd_clean(args, tools: Tools) -> int:
    """Remove each batch's worktree, recording every removal in the manifest as it lands.

    A removal is recorded rather than re-derived because the evidence is destroyed by the
    act: the remote leg reads the worktree, and the worktree is what it deletes. So the
    manifest is saved after each `removed:` verdict, a batch already carrying `removed_at`
    is skipped without an ssh call, and the file is deleted only once no batch is left.
    """
    from fanout_lib.clean import remote_clean_command

    run = manifest_mod.load(args.run_id, root=args.manifest_root)
    kept = []
    for i, b in enumerate(run.batches):
        if b.removed_at:
            print(f"{b.batch} on {b.host}: removed earlier ({b.removed_at})")
            continue
        try:
            proc = tools.run(b.host, remote_clean_command(b), 120.0, None)
        except subprocess.TimeoutExpired:
            print(f"{b.batch} on {b.host}: clean timed out")
            kept.append(b.batch)
            continue
        line = (proc.stdout or proc.stderr).strip()
        print(f"{b.batch} on {b.host}: {line}")
        if line.startswith("removed:"):
            run.batches[i] = dataclasses.replace(
                b, removed_at=datetime.now(UTC).isoformat()
            )
            manifest_mod.save(run, root=args.manifest_root)
        else:
            kept.append(b.batch)
    if kept:
        print(f"run {run.run_id}: kept {', '.join(kept)} — re-run clean once merged")
    else:
        manifest_mod.path(run.run_id, args.manifest_root).unlink(missing_ok=True)
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
    clean_parser = sub.add_parser("clean")
    clean_parser.add_argument("run_id")
    _add_manifest_root(clean_parser)
    clean_parser.set_defaults(fn=cmd_clean)
    # No `help=` here, matching every other subparser above: argparse only lists a
    # subcommand under "positional arguments" when its own help text is set, so leaving it
    # unset is what keeps `clean-one` out of `--help`'s body. `help=argparse.SUPPRESS`
    # looks like the right tool but isn't: argparse's subparser formatting doesn't filter a
    # SUPPRESS'd choice the way it does a SUPPRESS'd ordinary argument, so it would print a
    # literal "clean-one  ==SUPPRESS==" line instead of hiding it. Either way it still
    # appears in the `{...}` choices list on the usage line — no argparse option removes
    # that without also removing every visible subcommand from it.
    clean_one_parser = sub.add_parser("clean-one")
    clean_one_parser.add_argument("worktree")
    # `branch` is unused by cmd_clean_one itself (clean_one reads the branch straight off
    # the worktree it re-parses); it's a positional here only so `ps` on the host names the
    # batch, the same reason systemd-run's --unit does at launch.
    clean_one_parser.add_argument("branch")
    clean_one_parser.set_defaults(fn=cmd_clean_one)
    args = p.parse_args(argv)
    return args.fn(args, tools or Tools())


if __name__ == "__main__":
    sys.exit(main())
