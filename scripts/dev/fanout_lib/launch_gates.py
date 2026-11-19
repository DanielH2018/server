"""The refusals `launch` applies to a batch grouping before it spends an ssh connection.

Each gate prints its own reason and returns True to refuse, so `cmd_launch` reads as a list of
refusals rather than a nest of conditions. Three read the issue bodies: `refuse_shared_files`,
`refuse_live_shared_files` and `refuse_solo_only`. The rest read the placement and the live
run manifests.

The shared-file check exists because the triage step groups issues so no two batches share an
Ansible role, and that rule never covers the shared code under `scripts/`. Two batches with
different role groupings can both cite `scripts/diagnostics/probe_lib/alerts.py`, and two
agents then fix the same defect. The check reads the same body citations the orchestrator
groups by, so a grouping that missed one is refused here rather than discovered at merge.

`refuse_shared_files` compares the batches of one launch. `refuse_live_shared_files` compares
them with the batches other runs launched, which is the only check that spans two
coordinators: each groups its own project and neither reads the other's grouping.
"""

import subprocess
import sys
from collections.abc import Collection, Iterable, Mapping, Sequence
from pathlib import Path

# Reach the sibling package: a directly-invoked script gets only its own directory on
# sys.path, and pyproject's `pythonpath` is a pytest setting.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fanout_lib.brief import REQUIRED_LABEL
from fanout_lib.manifest import Batch, Manifest, live_batches, standing_batches
from findings_lib.issue_model import Issue
from fanout_lib.placement import HostReading
from fanout_lib.status import BatchStatus, parse_status
from fanout_lib.target import SERVER
from fanout_lib.transport import Tools, error_text, read_host
from findings_lib.issue_model import cited_paths
from findings_lib.solo_only import solo_only_reasons
from lib.inventory_lists import LANDING_HOST
from lib.cli_help import error, warn
from lib.role_paths import CLUSTER_VARS_PREFIX, SECRETS_FILE

# The host `fanout.py place` normally runs on; its launches go over `bash -c`, never ssh, so the
# per-host ssh budget below doesn't apply to it. The orchestrator runs on the deploy host, which
# is a coupling of convention: move one and this names the wrong host.
LOCAL_HOST = LANDING_HOST
# ufw limit ssh REJECTs a 6th connection to one host within 30s. Two of those five go to the
# headroom and health reads, leaving room for at most this many batch launches — each its
# own ssh connection — before the run risks the 6th.
MAX_BATCHES_PER_REMOTE_HOST = 3
# The memory cap, which is a DIFFERENT constraint from the ssh budget. An agent
# reserves RESERVATION_BYTES (2 GiB) against a login plane capped at 14 to 23 GiB
# (`claude_code_rc_memory_high`), which also holds the operator's own sessions. Unlike the ssh
# budget above, this one stacks ACROSS runs: a second launch minutes after the first measures
# a memory.current the first run's agents have not yet grown into, so headroom alone underprices
# them. Counted from the run manifests, which cost no ssh to read, less the batches whose
# unit has stopped (`still_running`).
MAX_LIVE_BATCHES_PER_HOST = 6
# DECIDED: the operator set both caps at 6 on 2026-11-13 to leave about 8 GiB of RAM free per
# node (`claude_code_fleet_memory_high` derives the floor). CPU on daniel-server (8 cores) is
# the known risk at 6: the earlier week measured a p95 of 6.0 of 8 cores at 4 batches.
LIVE_BATCH_CAP_BY_HOST = {"daniel-server": 6}


def live_batch_cap(host: str) -> int:
    """How many running batches `host` fits; `MAX_LIVE_BATCHES_PER_HOST` by default."""
    return LIVE_BATCH_CAP_BY_HOST.get(host, MAX_LIVE_BATCHES_PER_HOST)


def over_ssh_budget(placed: list[tuple[str, str]], multiplexed: bool = False) -> bool:
    """Print and return True when the placement puts too many batches on one remote host.

    A `multiplexed` transport spends one connection per host however many batches it
    launches (`transport.mux_opts`), so the budget does not bind and nothing is refused.
    """
    if multiplexed:
        return False
    counts: dict[str, int] = {}
    for _, host in placed:
        counts[host] = counts.get(host, 0) + 1
    over = False
    for host, n in counts.items():
        if host != LOCAL_HOST and n > MAX_BATCHES_PER_REMOTE_HOST:
            error(
                f"{n} batches would land on {host}, more than "
                f"{MAX_BATCHES_PER_REMOTE_HOST} fits the ssh budget there — split the "
                "fan-out"
            )
            over = True
    return over


def _stopped(s: BatchStatus) -> bool:
    """Whether `s` shows the batch's agent has stopped, so it no longer holds its reservation.

    `parse_status` reads a block that never arrived, or arrived without unit properties, as
    `failed` with no exit code. That is a read that failed rather than a unit that did, so it
    stays live. Only a unit that ended with an exit status counts as `failed` here.
    """
    return s.state != "running" and not (s.state == "failed" and s.exit_code is None)


def live_by_host(root: Path) -> dict[str, list[tuple[str, Batch]]]:
    """Every uncleaned batch under `root` by the host it runs on, each with its run id."""
    live: dict[str, list[tuple[str, Batch]]] = {}
    for run_id, b in live_batches(root).values():
        live.setdefault(b.host, []).append((run_id, b))
    return live


def still_running(
    standing: list[tuple[str, Batch]], unit_states: str
) -> list[tuple[str, Batch]]:
    """The `standing` (run id, batch) pairs whose agent has not stopped.

    The unit state is the oracle, the one `place status` reads: a finished agent holds no
    memory, and neither a standing worktree nor an unset `removed_at` says it has finished.
    Fails closed: a batch with no block in `unit_states`, or none at all, stays running, so a
    gate that cannot tell refuses as it always did.

    Args:
        standing: the host's uncleaned batches, each with the run id it was launched under.
        unit_states: the unit blocks `transport.read_host` took on the headroom connection,
            as `HostReading.unit_states` carries them; empty when none came back.
    """
    if not unit_states or not standing:
        return standing
    statuses = {s.batch: s for s in parse_status([b for _, b in standing], unit_states)}
    return [(run_id, b) for run_id, b in standing if not _stopped(statuses[b.batch])]


def live_batch_slots(
    root: Path, readings: Sequence[HostReading] = ()
) -> dict[str, int]:
    """How many more running batches each of `readings`' hosts fits before its live-batch cap.

    Counts as `over_live_batch_cap` does: the uncleaned batches under `root` that
    `still_running` finds running. `placement.place` takes this as its `slots`, so a batch
    falls back to a host with cap room instead of `over_live_batch_cap` refusing the host it
    preferred.

    Args:
        root: the manifest directory to read every run under.
        readings: the headroom readings `read_host` took, carrying each host's unit states.
    """
    live = live_by_host(root)
    return {
        r.host: live_batch_cap(r.host)
        - len(still_running(live.get(r.host, []), r.unit_states))
        for r in readings
    }


def over_live_batch_cap(
    placed: list[tuple[str, str]],
    root: Path,
    readings: Sequence[HostReading] = (),
) -> bool:
    """Print and return True when a host would end up holding too many running batches.

    `over_ssh_budget` above counts only THIS run, which is right for an ssh rate limit that
    refills in 30 seconds. Memory does not refill: a batch launched five minutes ago still
    holds its 2 GiB reservation, and `placement.headroom` cannot see the part of that
    reservation the agent has not yet grown into. So this counts the batches every manifest
    shows uncleaned — no `removed_at` — that `still_running` finds running, and adds this
    run's placements to them.

    A batch whose agent has finished holds no reservation, so it does not count, whether or
    not its worktree still stands, and on either host. The unit states come
    from `readings`: `read_host` asked for them on the headroom connection, which costs no ssh
    the budget lacks. A host with no reading, or one whose states did not come back, keeps
    every batch counted until `clean <run-id>` removes it.

    Args:
        placed: `place`'s (batch, host) pairs for this run.
        root: the manifest directory to read every run under.
        readings: the headroom readings `read_host` took, carrying each host's unit states.
    """
    states = {r.host: r.unit_states for r in readings}
    live = live_by_host(root)
    new: dict[str, int] = {}
    for _, host in placed:
        new[host] = new.get(host, 0) + 1
    over = False
    # DECIDED: no LOCAL_HOST exemption, unlike `over_ssh_budget` above. daniel-box is exempt
    # there because its launches go over `bash -c` and spend no ssh connection at all; that
    # reason does not transfer to memory, which a local agent consumes exactly as a remote one
    # does.
    for host, n in sorted(new.items()):
        standing = still_running(live.get(host, []), states.get(host, ""))
        if len(standing) + n <= live_batch_cap(host):
            continue
        held = ", ".join(sorted(f"{b.batch} in run {run_id}" for run_id, b in standing))
        error(
            f"{host} already holds {len(standing)} live batch(es) ({held or 'none'}) and "
            f"this run would place {n} more, over the {live_batch_cap(host)} that "
            "host's memory cap fits — wait for one to finish, run `clean <run-id>` on "
            "the finished ones, or split the fan-out"
        )
        over = True
    return over


def fetch_issues(
    tools: Tools, batches: dict[str, list[int]], repo: str = SERVER
) -> dict[int, Issue] | None:
    """Fetch every batch's issues up front, or return None having said why it refused.

    Every fetch completes before the first launch on purpose: a fetch that failed inside the
    placement loop left the batches already launched running under `auto`, in worktrees this
    slice locks, with no manifest naming them.
    """
    fetched: dict[int, Issue] = {}
    for number in [n for numbers in batches.values() for n in numbers]:
        try:
            fetched[number] = tools.gh_issue(number, repo)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            error(f"could not fetch issue {number}: {error_text(exc)}")
            return None
        except KeyError as exc:
            error(f"could not read issue {number}: gh's answer has no {exc} field")
            return None
        except ValueError as exc:
            error(f"could not read issue {number}: {exc}")
            return None
    unlabelled = [
        n for n, issue in fetched.items() if REQUIRED_LABEL not in issue.labels
    ]
    for number in unlabelled:
        error(
            f"issue {number} does not carry the `{REQUIRED_LABEL}` label — only "
            "issues findings.py filed are fan-out work"
        )
    return None if unlabelled else fetched


def live_elsewhere(batches: dict[str, list[int]], root: Path, repo: str) -> bool:
    """Print and return True when a new batch shares an issue with one still live.

    `exists_check_command` guards the host a batch is placed on, which is not the same
    question: placement is free to send a relaunch to the OTHER host, where no worktree of
    that name exists, and a second agent then starts on issues the first is still holding.
    The manifests are the only record that spans both hosts, and reading them costs no ssh.

    The test is the issue set, not the batch id, because the id is derived from the spec as
    typed: `--batch 1386,1345` and `--batch 1345` both miss a live `1345-1386` by name while
    putting a second agent on issues it holds. What must not happen twice is an issue, so
    that is what is compared.

    Only batches in the same repo are compared: issue numbers collide across repos, so a
    live dotfiles batch on #12 says nothing about server #12.

    Args:
        batches: `_parse_batches`' mapping of batch id to the issue numbers it would take.
        root: the manifest directory to read every run under.
        repo: the repo this launch works, as `OWNER/NAME`.
    """
    live = standing_batches(repo, root)
    refused = False
    for spec, issues in batches.items():
        for run, b in live:
            overlap = sorted(set(issues) & set(b.issues))
            if not overlap:
                continue
            shared = ", ".join(f"#{n}" for n in overlap)
            error(
                f"batch {spec} shares {shared} with batch {b.batch}, live in "
                f"run {run.run_id} on {b.host}; run clean {run.run_id} first"
            )
            refused = True
            break
    return refused


def batch_paths(
    issues: Iterable[Issue], tracked: Collection[str] = ()
) -> tuple[str, ...]:
    """The files `issues`' bodies cite, sorted, each resolved against `tracked`.

    One definition of what a batch was briefed on: the two shared-file gates compare it, and
    `launch` records it on the batch.
    """
    return tuple(
        sorted({path for issue in issues for path in cited_paths(issue.body, tracked)})
    )


def shared_files(
    batches: Mapping[str, Sequence[int]],
    issues: Mapping[int, Issue],
    tracked: Collection[str] = (),
) -> list[tuple[str, list[str]]]:
    """Every file cited by issues in two or more batches, with the batches that cite it.

    Args:
        batches: batch id → the issue numbers in it, as `_parse_batches` returns them.
        issues: every fetched issue by number; a number a batch names but this lacks is
            skipped, since `fetch_issues` has already refused that launch.
        tracked: the target repo's tracked files, which a path cited from a subdirectory
            resolves against. Empty leaves every path as the body spells it.

    Returns:
        `[(path, [batch, ...]), ...]` sorted by path, each batch list sorted. Empty when the
        grouping is clean.
    """
    cited: dict[str, set[str]] = {}
    for batch, numbers in batches.items():
        fetched = [issues[n] for n in numbers if n in issues]
        for path in batch_paths(fetched, tracked):
            cited.setdefault(path, set()).add(batch)
    return [
        (path, sorted(names)) for path, names in sorted(cited.items()) if len(names) > 1
    ]


def refuse_shared_files(
    batches: Mapping[str, Sequence[int]],
    issues: Mapping[int, Issue],
    allowed: Sequence[str] = (),
    tracked: Collection[str] = (),
) -> bool:
    """Whether `launch` must refuse this grouping, having printed every collision to stderr.

    Runs before the first ssh, like the label gate: a refusal here costs nothing. `allowed`
    is the `--allow-shared-file PATH` override, one path per use, for a citation that is
    context rather than an edit target — `docs/claude-tooling.md` cited by three findings
    that each edit a different script. It names the file it excuses, so a wave that trips on
    a doc path cannot switch the check off for the script collision beside it; that collision
    still refuses. An excused collision still prints, so the override is on the record.
    """
    collisions = shared_files(batches, issues, tracked)
    refused = False
    for path, names in collisions:
        if path in allowed:
            warn(
                f"batches {', '.join(names)} all cite {path} — allowed by "
                "--allow-shared-file"
            )
            continue
        refused = True
        error(
            f"batches {', '.join(names)} all cite {path} — two agents editing one "
            "file is the #1798 conflict; regroup them into one batch, or pass "
            f"--allow-shared-file {path} if the citation is context rather than an edit"
        )
    return refused


def excusable(path: str) -> bool:
    """Whether `--allow-shared-file` may excuse a cross-run collision on `path`.

    Never the SOPS file or a cluster-vars file. Each is one encrypted document, so two
    branches that both edit it conflict on every line and neither rebases over the other.
    """
    return path != SECRETS_FILE and not path.startswith(CLUSTER_VARS_PREFIX)


def _with_running_unit(tools: Tools, batches: Sequence[Batch]) -> set[tuple[str, str]]:
    """The (host, unit) of each of `batches` whose agent has not stopped.

    One read per host that holds one, with `still_running`'s rule: a host that cannot be
    read, or a unit with no block in the reply, counts as running.
    """
    running: set[tuple[str, str]] = set()
    for host in sorted({b.host for b in batches}):
        there = [("", b) for b in batches if b.host == host]
        reading = read_host(tools, host, [b for _, b in there])
        states = "" if isinstance(reading, str) else reading.unit_states
        running |= {(b.host, b.unit) for _, b in still_running(there, states)}
    return running


def refuse_live_shared_files(
    batches: Mapping[str, Sequence[int]],
    issues: Mapping[int, Issue],
    root: Path,
    repo: str,
    tools: Tools,
    allowed: Sequence[str] = (),
    tracked: Collection[str] = (),
) -> dict[str, tuple[str, ...]] | None:
    """Refuse a launch citing a file that a live batch in another run holds.

    A batch holds the files it cites while its unit is active or its branch has an open PR.
    A batch whose agent ended with no PR holds nothing, however long its worktree stands:
    "uncleaned" is not the rule, because a finished batch can stand for weeks.

    Reads the open PRs in one `gh pr list` call, and only when another run has an uncleaned
    batch. Reads a unit's state only for a batch with no open PR that could collide, at one
    connection per host holding one. A batch
    saved before `Batch.cited_paths` existed has its issue bodies fetched, once each. Every
    read that fails refuses the launch, because it cannot tell who holds what.

    The caller holds the launch lock (`fanout_lib.launch_lock`), so no other launch can add
    a batch between this read and the caller's own manifest save.

    Args:
        batches: batch id to the issue numbers it would take.
        issues: every fetched issue by number.
        root: the manifest directory to read every run under.
        repo: the repo this launch works; a path means nothing in another repo's tree.
        tools: the process boundary.
        allowed: the `--allow-shared-file` paths. One excuses a collision on that path
            unless `excusable` rules it out, and the collision is still printed.
        tracked: the repo's tracked files, which a cited fragment resolves against.

    Returns:
        None having printed why the launch is refused. Otherwise each new batch's excused
        paths, for `Batch.excused_paths`; a batch that needed no excuse has no entry.
    """
    new = {
        batch: set(batch_paths([issues[n] for n in numbers if n in issues], tracked))
        for batch, numbers in batches.items()
    }
    standing = standing_batches(repo, root)
    if not standing or not any(new.values()):
        return {}
    open_branches = tools.open_pr_branches(repo)
    if open_branches is None:
        error(
            f"could not read the open PRs of {repo}, so cannot tell which batches in "
            "other runs still hold their files — run launch again"
        )
        return None

    def could_collide(b: Batch) -> bool:
        if b.cited_paths is None:
            return True
        return any(paths & set(b.cited_paths) for paths in new.values())

    candidates = [(run, b) for run, b in standing if could_collide(b)]
    running = _with_running_unit(
        tools, [b for _, b in candidates if b.branch not in open_branches]
    )
    holders = [
        (
            run,
            b,
            "an open PR"
            if b.branch in open_branches
            else "an agent not read as stopped",
        )
        for run, b in candidates
        if b.branch in open_branches or (b.host, b.unit) in running
    ]
    collisions: list[tuple[str, str, Manifest, Batch, str]] = []
    for run, b, why in holders:
        held = b.cited_paths
        if held is None:
            try:
                held = batch_paths([tools.gh_issue(n, repo) for n in b.issues], tracked)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                error(
                    f"could not fetch the issues of batch {b.batch} (run {run.run_id}) to "
                    f"read the files it holds: {error_text(exc)}"
                )
                return None
        collisions += [
            (path, batch, run, b, why)
            for batch, paths in new.items()
            for path in sorted(paths & set(held))
        ]
    excused: dict[str, set[str]] = {}
    refused = []
    for path, batch, run, b, why in sorted(collisions, key=lambda c: c[:2]):
        holder = (
            f"batch {b.batch} holds it with {why} (run {run.run_id}, orchestrator "
            f"branch `{run.orchestrator_branch}`)"
        )
        if path in allowed and excusable(path):
            warn(
                f"{path}: batch {batch} cites it; {holder} — allowed by --allow-shared-file"
            )
            excused.setdefault(batch, set()).add(path)
            continue
        way_out = (
            f"pass --allow-shared-file {path} if the citation is context rather than an edit"
            if excusable(path)
            else "--allow-shared-file cannot excuse this path"
        )
        refused.append(f"{path}: batch {batch} cites it; {holder}; {way_out}")
    if not refused:
        return {batch: tuple(sorted(paths)) for batch, paths in excused.items()}
    error(
        f"{len(refused)} cited file(s) held by live batches in other runs — ask the "
        "coordinator on the orchestrator branch named to finish or release the batch, and "
        "do not clean another coordinator's run"
    )
    for line in refused:
        error(line)
    return None


def refuse_solo_only(
    batches: Mapping[str, Sequence[int]], issues: Mapping[int, Issue], repo: str
) -> bool:
    """Whether `launch` must refuse because a batch holds an issue citing the fan-out tooling.

    `next` marks such an issue `[solo-only]`. A batch that edits the pipeline runs
    under its own edited copy of the reviewer and the Stop hook, which is why
    the held-settings machinery exists. Refused before the first ssh, like the shared-file
    check, and a solo session claims and works the issue instead. `repo` is the register the
    batches come from; another repo's issue is never solo-only.
    """
    refused = False
    for batch, numbers in batches.items():
        for number in numbers:
            issue = issues.get(number)
            cited = solo_only_reasons(issue.title, issue.body, repo) if issue else []
            if not cited:
                continue
            refused = True
            error(
                f"batch {batch}: #{number} cites the fan-out tooling "
                f"({', '.join(cited)}) — it is solo-only (#3959); drop it from the batch "
                "and work it in a session of its own"
            )
    return refused
