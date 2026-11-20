"""Place issue-fanout batches on the session host with the most memory headroom.

Reads memory.current against MemoryHigh for BOTH cgroups an agent lives in — user.slice (the
fleet) and the running user's user-<uid>.slice (the login plane) — on daniel-box and daniel-server, scores each host
on the tighter of the two, picks the host with the most headroom per batch, creates a fresh
worktree there, and starts a headless agent, at the rung `--size` names
(`fanout_lib.sizing`), as a transient user service. daniel-box agents land their PR;
daniel-server agents stop at `gh pr create`. It shipped in commit bd8e62bf8.
`launch` runs `fanout.py review` unless given `--no-review`: a review between each batch's PR
and its landing. `fanout_lib/review/review.py` has the phases.

Usage::

    fanout.py place read
    fanout.py place launch --batch 1345,1386 [--batch 1288] [--host daniel-box] [--no-review]
    fanout.py place launch --repo DanielH2018/dotfiles --batch 763
    fanout.py place claim --batch 1345,1386 [--batch 1288]
    fanout.py place status <run-id>
    fanout.py place stop <run-id> [batch]
    fanout.py place resume <run-id> <batch> [--log PATH]
    fanout.py place clean <run-id>

One `launch` runs at a time for a user on a host: it holds `fanout_lib.launch_lock` from its
first gate to its last manifest save, so a second launch reads the batches the first placed.
`resume` holds the same lock for the same span.

`resume` restarts one finished batch whose PR's last landing ended `merge-conflict`, on the
batch's own host, against the same live-batch cap and memory reservation as a launch. Its
unit runs only the conflict resume of `fanout_lib/review/conflict_resume.py`.
`fanout_lib/resume.py` has what it refuses and where it reads the verdict.

`launch` claims this repo's issues itself, under the branch checked out where it runs, just
before each batch's agent starts; `claim` takes the same claims and launches nothing.
`fanout_lib/claims.py` has the rules.

Exit codes (`lib.exit_codes`'s `FANOUT_PLACE_*`); `status` ranks its batches' tiers 5, 1, 75, 0:
- 0: every batch launched with every issue claimed; `status`: none needs a hand.
- 1: `launch` refused before placing, or a batch failed to launch. `resume` refused, naming
  the reason and the command that fits, or its unit failed to start. `status`: a batch reads
  `parked` (its landing waits on a hold or rule that is not its own), `needs-input`,
  `no-pr`, `no-verdict` (land.sh printed no VERDICT) or `no-report`.
- 3: `launch` placed nothing: no host had headroom, a `fanout_lib.launch_gates` cap refused
  (the per-host ssh budget, or the live-batch memory cap across runs), or every claim was.
- 4: no host readable.
- 5: `status` only: a batch's unit exited non-zero or its session set `is_error`.
- 6: no candidate host signs commits GitHub verifies, or the signing keys were unreadable.
- 7: `launch` started batches but dropped at least one refused claim.
- 8: `launch` or `resume` waited 120 s for the launch lock (`fanout_lib.launch_lock`) while another
  launch held it, and did nothing.
- 64: a malformed `--batch`, an unregistered `--repo`, or a run-id with no manifest.
- 75: `status` only: a host's read timed out and no batch reported 1 or 5.

A launch costs one remote ssh connection per host it reads for headroom, a second only for
a host a batch is actually placed on (its session-health read), plus one
per batch placed there — worktree add+lock, venv sync, brief write and systemd-run folded into one
call. `--host` pins every batch to one host; leave it unset and placement reads both hosts
and chooses per batch. Calls to the host this script itself runs on go over `bash -c`, not
ssh, so they never count against `ufw limit ssh` — `fanout_lib.launch_gates` holds the cap
that keeps a placement on an actual remote host under it.

`--repo` launches in another register `findings.py --repo` can judge claims for, such as the
dotfiles repo. Its trees go in that repo's checkout, from its default branch. Its batches run
on this host only, because `findings.py` judges a claim in that register against this host's
checkout of it. `launch` claims each such batch itself, under the batch's own branch, once the
tree is locked and before the agent starts. Its agents stop at the PR on every host, and
`status` reads a PR URL as `done` without a `VERDICT:` line. The orchestrator lands those PRs
with the repo's own tooling.

Launch locks each worktree with reason `fanout-<batch>` so a merged-worktree prune cannot
remove it while the unit still runs; `clean <run-id>` is the escape — it removes a batch's
worktree, unlocking it in the process, once the batch's PR merged and the tree is clean, and
leaves a batch that isn't ready to go both locked and in the manifest.
"""

import argparse
import dataclasses
import json
from datetime import UTC, datetime
from pathlib import Path


from dev.fanout_lib import claims as claims_mod
from dev.fanout_lib.abandon import cmd_abandon, cmd_stop
from dev.fanout_lib.resume import add_resume_arguments, cmd_resume
from dev.fanout_lib import launch as launch_mod
from dev.fanout_lib import launch_lock
from dev.fanout_lib import manifest as manifest_mod
from dev.fanout_lib import signing as signing_mod
from dev.fanout_lib import sizing
from dev.fanout_lib import status as status_mod
from dev.fanout_lib.status_view import one_line, runs_lines, status_lines
from dev.fanout_lib.brief import render_brief
from dev.findings_lib.tracked_paths import tracked_files
from dev.fanout_lib.launch_gates import (
    batch_paths,
    fetch_issues,
    live_elsewhere,
    live_by_host,
    live_batch_slots,
    over_live_batch_cap,
    over_ssh_budget,
    refuse_live_shared_files,
    refuse_shared_files,
    refuse_solo_only,
)
from lib.cli_help import ArgumentParser, error, warn
from lib.exit_codes import (
    FANOUT_PLACE_BAD_ARGS,
    FANOUT_PLACE_FAILED,
    FANOUT_PLACE_LOCK_TIMEOUT,
    FANOUT_PLACE_NO_HOST_READABLE,
    FANOUT_PLACE_NO_SIGNING_HOST,
    FANOUT_PLACE_NOTHING_PLACED,
    FANOUT_PLACE_OK,
    FANOUT_PLACE_PARTLY_LAUNCHED,
)
from lib.git import git
from dev.fanout_lib.session_banner import health_lines
from dev.fanout_lib.placement import HostReading, NoHeadroom, place
from dev.fanout_lib.target import SERVER, for_launch, registered_repos, resolve
from dev.fanout_lib.transport import (
    HOSTS,
    Tools,
    this_host,
    read_host,
    registered_keys,
    verified_hosts,
)


def _readings(tools: Tools, hosts, root=None):
    """Read each host's headroom; the unit states of `root`'s batches ride it."""
    live = live_by_host(root) if root else {}
    good, bad = [], []
    for host in hosts:
        r = read_host(tools, host, [b for _, b in live.get(host, [])])
        (bad if isinstance(r, str) else good).append(r)
    return good, bad


def cmd_read(args, tools: Tools) -> int:
    good, bad = _readings(tools, HOSTS)
    # `signing=` makes the launch gate's verdict readable before a launch spends an agent on
    # it. `unknown` means the registered-key read itself failed, which `launch` refuses on.
    registered = registered_keys(tools)
    for r in good:
        # Both caps, because placement scores the tighter of the two: a fleet number with
        # room says nothing on its own about whether a batch fits.
        print(
            f"{r.host}: fleet cap={r.cap_bytes} current={r.current_bytes} "
            f"plane cap={r.plane_cap_bytes} current={r.plane_current_bytes} "
            f"agents={r.live_agents} "
            f"signing={signing_mod.read_verdict(r.host, r.signing_key, registered)}"
        )
    if isinstance(registered, str):
        warn(registered)
    for msg in bad:
        warn(msg)
    return FANOUT_PLACE_OK if good else FANOUT_PLACE_NO_HOST_READABLE


def candidate_readings(
    tools: Tools, registered: frozenset[str], hosts: list[str], root: Path
) -> list[HostReading] | int:
    """The readings of the hosts a batch may go to, or the exit code that refuses the launch.

    Warns on stderr for each host that drops out, and prints an error when none is left.
    `launch` and the `fanout-headroom` wait source (`fanout_lib.headroom_probe`) both call
    this, so the wait reads the hosts exactly as the launch it waits for will.

    Args:
        tools: the process boundary.
        registered: the account's registered signing keys, as `registered_keys` read them.
        hosts: the hosts to read, or the one `--host` pins.
        root: the manifest directory, whose uncleaned batches' unit states ride the read.

    Returns:
        The readable hosts that sign verifiably, or `FANOUT_PLACE_NO_HOST_READABLE` or
        `FANOUT_PLACE_NO_SIGNING_HOST`.
    """
    good, bad = _readings(tools, hosts, root)
    for msg in bad:
        warn(f"placing without {msg}")
    if not good:
        return FANOUT_PLACE_NO_HOST_READABLE
    good, refusals = verified_hosts(good, registered)
    for reason in refusals:
        warn(f"not placing on {reason}")
    if not good:
        error(
            "no candidate host signs commits GitHub verifies — a batch placed there "
            "opens a PR that cannot merge"
        )
        return FANOUT_PLACE_NO_SIGNING_HOST
    return good


def placement(
    batches: list[str],
    good: list[HostReading],
    pin: str | None,
    root: Path,
    multiplexed: bool,
) -> list[tuple[str, str]] | None:
    """Each batch's host, or None having printed why `launch` would place nothing.

    Spends no ssh: it decides from `good`, the readings `candidate_readings` took, and the
    manifests under `root`. The `fanout-headroom` wait source calls it with placeholder
    batch names, which is why its verdict and `launch`'s exit 3 cannot disagree.
    """
    slots = live_batch_slots(root, good)
    try:
        placed = place(batches, good, pin=pin, slots=slots)
    except NoHeadroom as exc:
        # Capped hosts were skipped, so a refusal may name the cap rather than memory. Without
        # the cap, placement succeeding means memory was not the reason: the cap gate then
        # names each host that cannot fit the whole run, and the batches it holds.
        try:
            place(batches, good, pin=pin)
        except NoHeadroom:
            error(str(exc))
            return None
        full = [h for h, n in slots.items() if n < len(batches) and h in (pin or h)]
        over_live_batch_cap([(b, h) for h in sorted(full) for b in batches], root, good)
        return None
    if over_ssh_budget(placed, multiplexed) or over_live_batch_cap(placed, root, good):
        return None
    return placed


def cmd_claim(args, tools: Tools) -> int:
    """Claim every batch's issues under the orchestrator's branch, launching nothing."""
    batches = claims_mod.parse_batches(args.batch)
    if batches is None:
        return FANOUT_PLACE_BAD_ARGS
    return claims_mod.claim_all(tools, batches, resolve(SERVER, tools.default_ref))


def cmd_launch(args, tools: Tools) -> int:
    batches = claims_mod.parse_batches(args.batch)
    if batches is None:
        return FANOUT_PLACE_BAD_ARGS
    sizes = sizing.parse_sizes(args.size, batches)
    if isinstance(sizes, str):
        error(sizes)
        return FANOUT_PLACE_BAD_ARGS
    try:
        target, args.host = for_launch(
            args.repo, args.host, this_host(), tools.default_ref
        )
    except ValueError as exc:
        error(str(exc))
        return FANOUT_PLACE_BAD_ARGS

    def name_run() -> str:
        return manifest_mod.unused_run_id(datetime.now(UTC), args.manifest_root)

    try:
        with launch_lock.held(args.manifest_root, name_run, args.lock_wait) as run_id:
            return _launch_locked(args, tools, batches, sizes, target, run_id)
    except launch_lock.LockTimeout as exc:
        error(
            f"{exc} after a {args.lock_wait:g} s wait — nothing was read, claimed or "
            "placed; run the same launch again once that launch has ended"
        )
        return FANOUT_PLACE_LOCK_TIMEOUT


def _launch_locked(
    args, tools: Tools, batches: dict[str, list[int]], sizes, target, run_id: str
) -> int:
    """Gate, place and start `batches` as run `run_id`; the caller holds the launch lock.

    Every read of the run manifests and every save of this run's own happens in here, which
    is what the lock makes one launch at a time.
    """
    holder = claims_mod.orchestrator_branch(tools, target)
    if holder is None:
        return FANOUT_PLACE_FAILED
    if live_elsewhere(batches, args.manifest_root, target.repo):
        return FANOUT_PLACE_FAILED
    fetched = fetch_issues(tools, batches, target.repo)
    if fetched is None:
        return FANOUT_PLACE_FAILED
    if refuse_solo_only(batches, fetched, target.repo):
        return FANOUT_PLACE_FAILED
    tracked = tracked_files() if target.is_server else ()
    if refuse_shared_files(batches, fetched, args.allow_shared_file, tracked):
        return FANOUT_PLACE_FAILED
    excused = refuse_live_shared_files(
        batches,
        fetched,
        args.manifest_root,
        target.repo,
        tools,
        args.allow_shared_file,
        tracked,
    )
    if excused is None:
        return FANOUT_PLACE_FAILED
    # Read the registered keys before the first ssh: a gh outage then refuses having spent no
    # connection against the per-host ssh limit.
    registered = registered_keys(tools)
    if isinstance(registered, str):
        error(f"{registered} — refusing to launch")
        return FANOUT_PLACE_NO_SIGNING_HOST
    hosts = [args.host] if args.host else list(HOSTS)
    good = candidate_readings(tools, registered, hosts, args.manifest_root)
    if isinstance(good, int):
        return good
    placed = placement(
        list(batches), good, args.host, args.manifest_root, tools.multiplexed()
    )
    if placed is None:
        return FANOUT_PLACE_NOTHING_PLACED
    # Only the hosts a batch landed on. Reading every host placement looked at spends an ssh
    # connection (`ufw limit ssh` allows 5 per 30s) on a host the signing gate or a failed
    # headroom read dropped, and puts its banner state into briefs for agents that never run
    # there. Sorted: set order is not stable across processes, and the lines go in verbatim.
    health = [
        ln
        for host in sorted({host for _, host in placed})
        for ln in health_lines(tools, host)
    ]
    # After every gate, like the claims: a refused launch writes nothing to the register.
    claims_mod.reap_register(tools, target)
    run = manifest_mod.Manifest(run_id, holder, [])
    refused = False
    for batch, host in placed:
        numbers = batches[batch]
        allowed_here = excused.get(batch, ())
        chosen = sizes.get(batch)
        if target.is_server:
            # Claimed here, after every gate, so a refusal above leaves nothing claimed,
            # and per batch, so a failed launch below leaves no later batch claimed.
            batch, numbers, dropped = claims_mod.claim_batch(
                tools, batch, numbers, holder
            )
            refused = refused or dropped
            if not numbers:
                continue
        issues = [fetched[n] for n in numbers]
        rung = sizing.size([label for i in issues for label in i.labels], chosen)
        brief = render_brief(
            issues, host, batch, holder, health, target, args.review, tracked
        )
        try:
            launched = launch_mod.launch(
                tools, host, batch, brief, numbers, target, args.review, rung=rung
            )
            cited = batch_paths(issues, tracked)
            launched = dataclasses.replace(
                launched,
                cited_paths=cited,
                excused_paths=tuple(p for p in allowed_here if p in cited),
            )
            run.batches.append(launched)
            # Saved per batch, not once after the loop: a launch killed between batches
            # (a tool timeout, a ^C) otherwise leaves running agents no manifest names,
            # so `status` and `clean` cannot find them.
            manifest_mod.save(run, root=args.manifest_root)
        except launch_mod.LaunchError as exc:
            released = (
                claims_mod.release_for_orchestrator(tools, numbers, holder)
                if target.is_server
                else ""
            )
            # The remote stderr a LaunchError carries spans lines, and the prefix has to
            # lead every line a grep for it should find.
            error(" ".join(f"{batch} on {host}: {exc}{released}".split()))
            # The refusal is per batch, after placement, so earlier batches are already
            # running. Name them and the run-id: the manifest is what `status` and `clean`
            # read, and the `clean <run-id>` an `exists` refusal asks for needs the id. A
            # run that launched nothing gets no manifest — an empty one records no work,
            # and `launch` reads every manifest under the root before it places.
            if not run.batches:
                error("launched before this failure: none")
                return FANOUT_PLACE_FAILED
            manifest_mod.save(run, root=args.manifest_root)
            launched = ", ".join(b.batch for b in run.batches)
            error(f"launched before this failure: {launched} (run {run.run_id})")
            return FANOUT_PLACE_FAILED
        claims_mod.post_worked_by(tools, numbers, launched.branch, target)
    if not run.batches:
        error("launched nothing: every issue's claim was refused")
        return FANOUT_PLACE_NOTHING_PLACED
    manifest_mod.save(run, root=args.manifest_root)
    pairs = ", ".join(f"{b.batch} -> {b.host} ({b.rung})" for b in run.batches)
    # `findings.py release` names the holder, so print it where the orchestrator reads.
    held = f"; claims held under `{holder}`" if target.is_server else ""
    print(f"run {run.run_id}: {pairs}{held}")
    # Not 3, which says nothing was placed: these batches are running.
    return FANOUT_PLACE_PARTLY_LAUNCHED if refused else FANOUT_PLACE_OK


def cmd_status(args, tools: Tools) -> int:
    run = manifest_mod.load(args.run_id, root=args.manifest_root)
    rows, worst = status_mod.collect(run, tools, one_line)
    if args.json:
        print(json.dumps(rows))
    else:
        print("\n".join(status_lines(rows, args.all)))
    return worst


def cmd_runs(args, tools: Tools) -> int:
    """List the run manifests on this host, so a lost run-id is recoverable."""
    runs = [
        r
        for r in manifest_mod.all_runs(args.manifest_root)
        if not args.orchestrator or r.orchestrator_branch == args.orchestrator
    ]
    if args.json:
        print(json.dumps([r.to_json() for r in runs]))
        return 0
    if not runs:
        whose = f" for {args.orchestrator}" if args.orchestrator else ""
        print(f"no fan-out runs{whose} under {args.manifest_root}")
        return 0
    standing = [b for r in runs for b in r.batches if not b.removed_at]
    held = status_mod.landing_holds(standing, tools)
    print("\n".join(runs_lines(runs, args.all, held, one_line)))
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
    brancher=None,
    clean=None,
) -> int:
    """Hidden: runs ON the host holding the worktree. `clean` calls it over Tools.run.

    `list_worktrees`, `ask`, `dirty`, `remover`, `unlocker`, `locker`, `brancher` and
    `clean` are seams — parameters rather than patched module attributes, so a test can
    drive this without pinning a first-party module name. Every default reaches the real
    thing, and all six of `clean_one`'s own seams are forwarded so a REMOVABLE-with-lock case
    run through this entry point stays hermetic too. `clean` replaces `clean_one` itself, so
    a test can read the default `ask` this function builds.

    An absent worktree reads `removed`, not `kept` — it is the goal state, not a failure. This
    is not how `clean` handles a gone tree: the copy of this script that a remote leg runs
    lives inside the worktree, so `remote_clean_command`'s shell chain answers the absent-tree
    case before this interpreter could start (Ruling 30). The path below stays for a
    `clean-one` run by hand against a tree that is already gone.
    """
    import functools

    from dev.fanout_lib.clean import clean_one as default_clean
    from dev.fanout_lib.clean import delete_branch as default_brancher
    from dev.fanout_lib.clean import lock as default_locker
    from dev.fanout_lib.clean import unlock as default_unlocker
    from lib.worktrees import is_dirty, is_merged, parse_worktree_list, remove

    target = resolve(args.repo)
    checkout = target.checkout

    if list_worktrees is None:

        def list_worktrees():
            return git("worktree", "list", "--porcelain", cwd=checkout).stdout

    # Resolved paths, not exact string equality: a worktree launched through one spelling
    # of REPO (a symlink, say) is still the tree `git worktree list` names by its target.
    wanted = Path(args.worktree).resolve()
    tree = next(
        (
            t
            for t in parse_worktree_list(list_worktrees())
            if Path(t.path).resolve() == wanted
        ),
        None,
    )
    if tree is None:
        print(f"removed: {args.worktree} (already gone)")
        return 0
    state, why = (clean if clean is not None else default_clean)(
        checkout,
        tree,
        ask=ask if ask is not None else functools.partial(is_merged, base=target.base),
        dirty=dirty if dirty is not None else is_dirty,
        remover=remover if remover is not None else remove,
        unlocker=unlocker if unlocker is not None else default_unlocker,
        locker=locker if locker is not None else default_locker,
        brancher=brancher if brancher is not None else default_brancher,
    )
    print(f"{state}: {args.worktree} {why}".rstrip())
    return 0


def cmd_clean(args, tools: Tools) -> int:
    """Remove each batch's worktree through `clean.clean_run`, and say what is left.

    Exits 1 when a leg reached no verdict, because a kept tree and an unreachable host need
    opposite things from the operator.
    """
    from dev.fanout_lib.clean import clean_run

    run = manifest_mod.load(args.run_id, root=args.manifest_root)
    kept, failed = clean_run(run, tools, args.manifest_root)
    if failed:
        print(f"clean failed for {', '.join(failed)} — see above")
    if kept:
        print(f"run {run.run_id}: kept {', '.join(kept)} — re-run clean once merged")
    return 1 if failed else 0


def _add_manifest_root(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--manifest-root",
        type=Path,
        default=manifest_mod.MANIFEST_DIR,
        help=argparse.SUPPRESS,
    )


def main(argv=None, tools: Tools | None = None) -> int:
    p = ArgumentParser(
        prog="fanout.py place", description=__doc__.splitlines()[0], allow_abbrev=False
    )
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
        "--repo",
        choices=registered_repos(),
        default=SERVER,
        help="the register whose issues the batches work; another repo's batches run on "
        "this host, are claimed by launch under their own branch, and stop at the PR",
    )
    launch_parser.add_argument(
        "--allow-shared-file",
        action="append",
        default=[],
        metavar="PATH",
        help="excuse this one file from the shared-file refusals, within this launch and "
        "against live batches in other runs (its citation is context, not an edit target); "
        "repeatable, and the collision is still printed. Never excuses secrets/secrets.yml "
        "or a secrets/cluster-vars file held by another run",
    )
    launch_parser.add_argument(
        "--lock-wait",
        type=float,
        default=launch_lock.WAIT_S,
        help=argparse.SUPPRESS,
    )
    launch_parser.add_argument(
        "--review",
        # DECIDED: on by default (#3675, operator 2026-10-09). `--no-review` is the way out,
        # for a batch whose review would cost more than it can catch.
        action=argparse.BooleanOptionalAction,
        default=True,
        help="review each batch's PR in a separate session before it lands (the default; "
        "--no-review skips it)",
    )
    launch_parser.add_argument(
        "--size",
        action="append",
        default=[],
        metavar="BATCH=RUNG",
        help=sizing.SIZE_HELP,
    )
    _add_manifest_root(launch_parser)
    launch_parser.set_defaults(fn=cmd_launch)
    claim_parser = sub.add_parser("claim")
    claim_parser.add_argument(
        "--batch",
        action="append",
        required=True,
        help="issue numbers joined by commas; repeatable",
    )
    claim_parser.set_defaults(fn=cmd_claim)
    run_parsers = {}
    for name, fn in (
        ("status", cmd_status),
        ("stop", cmd_stop),
        ("clean", cmd_clean),
        ("abandon", cmd_abandon),
        ("resume", cmd_resume),
    ):
        run_parsers[name] = sub.add_parser(name)
        run_parsers[name].add_argument("run_id")
        _add_manifest_root(run_parsers[name])
        run_parsers[name].set_defaults(fn=fn)
    runs_parser = sub.add_parser("runs")
    runs_parser.add_argument("--json", action="store_true")
    runs_parser.add_argument("--orchestrator", metavar="BRANCH")
    runs_parser.add_argument(
        "--all",
        action="store_true",
        help="every run and batch, cleaned ones included; the default lists standing batches",
    )
    _add_manifest_root(runs_parser)
    runs_parser.set_defaults(fn=cmd_runs)
    run_parsers["stop"].add_argument("batch", nargs="?")
    run_parsers["status"].add_argument(
        "--json", action="store_true", help="one object per batch, for fanout.py probe"
    )
    run_parsers["status"].add_argument(
        "--all",
        action="store_true",
        help="one full line per batch; the default counts landed and cleaned batches and "
        "shortens a done batch to its PR URL",
    )
    run_parsers["abandon"].add_argument("batch")
    add_resume_arguments(run_parsers["resume"])
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
    # `branch` is unused by cmd_clean_one, which reads the branch off the worktree; it is a
    # positional only so `ps` on the host names the batch, as systemd-run's --unit does.
    clean_one_parser.add_argument("branch")
    clean_one_parser.add_argument("--repo", default=SERVER)
    clean_one_parser.set_defaults(fn=cmd_clean_one)
    args = p.parse_args(argv)
    # The manifest-reading subcommands, checked here rather than caught as a
    # FileNotFoundError around the call: a mistyped run-id is a usage error and deserves a
    # line, while an FileNotFoundError raised deeper inside one of them is a real fault and
    # must keep its traceback. `launch` refuses with "run clean <run-id> first", so a
    # mistyped id at that prompt is the likely way in.
    if args.cmd in run_parsers:
        if not manifest_mod.exists(args.run_id, args.manifest_root):
            error(f"no manifest for run {args.run_id} under {args.manifest_root}")
            return FANOUT_PLACE_BAD_ARGS
    return args.fn(args, tools or Tools())
