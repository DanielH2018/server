"""The refusals `launch` applies to a batch grouping before it spends an ssh connection.

Each gate prints its own reason and returns True to refuse, so `cmd_launch` reads as a list of
refusals rather than a nest of conditions. Two read the issue bodies: `refuse_shared_files`
and `refuse_solo_only`. The rest read the placement and the live run manifests.

The shared-file check exists because the triage step groups issues so no two batches share an
Ansible role, and that rule never covers the shared code under `scripts/`. Two batches with
different role groupings can both cite `scripts/diagnostics/probe_lib/alerts.py`, and two
agents then fix the same defect. The check reads the same body citations the orchestrator
groups by, so a grouping that missed one is refused here rather than discovered at merge.
"""

import sys
from collections.abc import Collection, Mapping, Sequence
from pathlib import Path

# Reach the sibling package: a directly-invoked script gets only its own directory on
# sys.path, and pyproject's `pythonpath` is a pytest setting.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fanout_lib.brief import Issue
from fanout_lib.manifest import live_batches
from findings_lib.issue_model import cited_paths
from findings_lib.solo_only import fanout_tooling_paths
from findings_lib.tracked_paths import resolve_fragments
from lib.ansible_inventory import GITOPS_HOST

# The host `fanout_place` normally runs on; its launches go over `bash -c`, never ssh, so the
# per-host ssh budget below doesn't apply to it. The orchestrator runs on the deploy host, which
# is a coupling of convention: move one and this names the wrong host.
LOCAL_HOST = GITOPS_HOST
# ufw limit ssh REJECTs a 6th connection to one host within 30s. Two of those five go to the
# headroom and health reads, leaving room for at most this many batch launches — each its
# own ssh connection — before the run risks the 6th.
MAX_BATCHES_PER_REMOTE_HOST = 3
# The memory cap, which is a DIFFERENT constraint that happens to share the number. An agent
# reserves RESERVATION_BYTES (2.5 GiB) and daniel-server's login plane is capped at 11 GiB
# (`claude_code_rc_memory_high`), so four live agents fit and five do not. Unlike the ssh
# budget above, this one stacks ACROSS runs: a second launch minutes after the first measures
# a memory.current the first run's agents have not yet grown into, so headroom alone underprices
# them. Counted from the run manifests, which cost no ssh to read.
MAX_LIVE_BATCHES_PER_HOST = 3


def over_ssh_budget(placed: list[tuple[str, str]]) -> bool:
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


def over_live_batch_cap(placed: list[tuple[str, str]], root: Path) -> bool:
    """Print and return True when a host would end up holding too many live batches.

    `over_ssh_budget` above counts only THIS run, which is right for an ssh rate limit that
    refills in 30 seconds. Memory does not refill: a batch launched five minutes ago still
    holds its 2.5 GiB reservation, and `placement.headroom` cannot see the part of that
    reservation the agent has not yet grown into. So this counts the batches every manifest
    still shows live — no `removed_at` — and adds this run's placements to them.

    That makes `clean <run-id>` load-bearing: a batch whose worktree is still standing counts
    against the host whether or not its agent is still running, so a run left uncleaned
    eventually refuses the next launch. The message names `clean` for that reason.

    Args:
        placed: `place`'s (batch, host) pairs for this run.
        root: the manifest directory to read every run under.
    """
    live: dict[str, list[str]] = {}
    for run_id, b in live_batches(root).values():
        live.setdefault(b.host, []).append(f"{b.batch} in run {run_id}")
    new: dict[str, int] = {}
    for _, host in placed:
        new[host] = new.get(host, 0) + 1
    over = False
    # DECIDED: no LOCAL_HOST exemption, unlike `over_ssh_budget` above. daniel-box is exempt
    # there because its launches go over `bash -c` and spend no ssh connection at all; that
    # reason does not transfer to memory, which a local agent consumes exactly as a remote one
    # does. The two caps share the number 3 and nothing else.
    for host, n in sorted(new.items()):
        standing = live.get(host, [])
        if len(standing) + n <= MAX_LIVE_BATCHES_PER_HOST:
            continue
        held = ", ".join(sorted(standing)) or "none"
        print(
            f"launch: {host} already holds {len(standing)} live batch(es) ({held}) and this "
            f"run would place {n} more, over the {MAX_LIVE_BATCHES_PER_HOST} that host's "
            "memory cap fits — run `clean <run-id>` on the finished ones, or split the "
            "fan-out",
            file=sys.stderr,
        )
        over = True
    return over


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
    live dotfiles batch on #763 says nothing about server #763.

    Args:
        batches: `_parse_batches`' mapping of batch id to the issue numbers it would take.
        root: the manifest directory to read every run under.
        repo: the repo this launch works, as `OWNER/NAME`.
    """
    live = [entry for entry in live_batches(root).values() if entry[1].repo == repo]
    refused = False
    for spec, issues in batches.items():
        for run_id, b in live:
            overlap = sorted(set(issues) & set(b.issues))
            if not overlap:
                continue
            shared = ", ".join(f"#{n}" for n in overlap)
            print(
                f"launch: batch {spec} shares {shared} with batch {b.batch}, live in "
                f"run {run_id} on {b.host}; run clean {run_id} first",
                file=sys.stderr,
            )
            refused = True
            break
    return refused


def shared_files(
    batches: Mapping[str, Sequence[int]],
    issues: Mapping[int, Issue],
    tracked: Collection[str] = (),
) -> list[tuple[str, list[str]]]:
    """Every file cited by issues in two or more batches, with the batches that cite it.

    Args:
        batches: batch id → the issue numbers in it, as `_parse_batches` returns them.
        issues: every fetched issue by number; a number a batch names but this lacks is
            skipped, since `_fetch_issues` has already refused that launch.
        tracked: the target repo's tracked files, which a path cited from a subdirectory
            resolves against (#4232). Empty leaves every path as the body spells it.

    Returns:
        `[(path, [batch, ...]), ...]` sorted by path, each batch list sorted. Empty when the
        grouping is clean.
    """
    cited: dict[str, set[str]] = {}
    for batch, numbers in batches.items():
        for number in numbers:
            issue = issues.get(number)
            if issue is None:
                continue
            for path in resolve_fragments(cited_paths(issue.body), tracked):
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
            print(
                f"launch: batches {', '.join(names)} all cite {path} — allowed by "
                "--allow-shared-file",
                file=sys.stderr,
            )
            continue
        refused = True
        print(
            f"launch: batches {', '.join(names)} all cite {path} — two agents editing one "
            "file is the #1798 conflict; regroup them into one batch, or pass "
            f"--allow-shared-file {path} if the citation is context rather than an edit",
            file=sys.stderr,
        )
    return refused


def refuse_solo_only(
    batches: Mapping[str, Sequence[int]], issues: Mapping[int, Issue]
) -> bool:
    """Whether `launch` must refuse because a batch holds an issue citing the fan-out tooling.

    `next` marks such an issue `[solo-only]` (#3959). A batch that edits the pipeline runs
    under its own edited copy of the reviewer, the red gate and the Stop hook, which is why
    the held-settings machinery exists. Refused before the first ssh, like the shared-file
    check, and a solo session claims and works the issue instead.
    """
    refused = False
    for batch, numbers in batches.items():
        for number in numbers:
            issue = issues.get(number)
            cited = fanout_tooling_paths(issue.body) if issue else []
            if not cited:
                continue
            refused = True
            print(
                f"launch: batch {batch}: #{number} cites the fan-out tooling "
                f"({', '.join(cited)}) — it is solo-only (#3959); drop it from the batch "
                "and work it in a session of its own",
                file=sys.stderr,
            )
    return refused
