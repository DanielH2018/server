#!/usr/bin/env python3
"""Drive the staging gate over a run of real master commits and report whether it is
trustworthy enough to block on.

This answers part 1 of slice 4's entry condition (`docs/staging-phase-c.md`): N consecutive
gate runs against real master SHAs with zero FALSE failures. It exists because the original
condition — collect the rate from real ticks — cannot be met. `consult_staging` runs only
when a tick carries `cs.k8s_deploy`, which needs a service that is in the staging subset AND
auto-deployable AND image-pin-bumped by that commit; measured 2026-08-29 that is about one
sample a month. The false-failure rate is a property of the gate MECHANISM rather than of
Renovate's schedule, so it is measured deliberately here instead.

WHAT A RUN LOOKS LIKE, AND WHY IT IS NOT THE AUTO-DEPLOY SET. Each commit is gated on the
services it actually CHANGED intersected with the staging subset, not on the narrower set a
tick would have promoted. That is deliberate and is what makes the measurement possible: the
mechanism under test — ssh, dispatcher, prep, ff-merge, deploy, expectations — is identical
either way, and gating only image-bump commits would reproduce the one-a-month problem this
script exists to escape. The difference is recorded rather than hidden: a run here proves the
gate can answer about a commit, not that a tick would have asked.

OLDEST TO NEWEST, ALWAYS. The staging checkout only moves forward — `staging_gate_remote.sh`
fast-forwards it — so walking newest-first would ask about commits older than its HEAD. Until
2026-08-30 that returned a verdict about the WRONG TREE with exit 0, because
`git merge --ff-only <ancestor>` succeeds and leaves HEAD alone. The remote script now asserts
HEAD equals the SHA under test, so the failure is loud, but the ordering is still what makes
the runs meaningful rather than merely non-silent.

A BACKFILL IS NOT IDEMPOTENT, for the same reason. One reset of the gate's checkout buys one
run: the tree ends the run at the newest planned commit, so a second pass over the same window
is all ancestors. A run that dies partway needs another reset before it can be resumed, because
the tree is already past the commits that failed. `--allow-ancestors` exists only to let an
operator watch that failure happen deliberately.

THE LEDGER IS THE POINT, not any single invocation. History supplies far fewer usable samples
than the entry condition asks for — measured 2026-08-30, eleven of the last 400 master commits,
once the era filter below is applied. So `--jsonl` is read back as well as written: runs
accumulate across invocations and
the condition ratchets toward MET as future commits are gated, rather than needing a third
rescope down to whatever one backfill happened to yield.

THE ERA FILTER. A commit predating its role's staging support deploys prod-shaped config to a
cluster that cannot take it, and comes back REJECTED. That is neither a gate misfire nor a
defect in the commit, so it has no honest triage answer and would block the verdict forever.
`staged_services_at` reads the staging inventory AT EACH COMMIT and gates only the services the
cluster ran then, which excludes those commits by construction instead of asking a human to
attribute them.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(
    0, str(_HERE.parents[1] / "ansible" / "roles" / "setup" / "gitops_deploy" / "files")
)


import yaml  # noqa: E402

import deploy_logic  # noqa: E402
import staging_gate  # noqa: E402

# The staging cluster's own inventory. Read per-commit rather than from the working tree: which
# services staging ran is a property of the SHA under test, not of today.
STAGE_INVENTORY = "ansible/inventory/host_vars/daniel-stage.yml"

# Where the gate's checkout lives, and the host it lives on. Duplicated from
# staging_gate_remote.sh, which is the copy that runs; this one only reads HEAD to refuse a plan
# the far side would reject.
STAGE_HOST = "daniel-server"
STAGE_REPO = "/home/ubuntu/server-staging"

# staging_gate.py's own exit vocabulary, reused rather than restated.
PASS = staging_gate.PASS
REJECTED = staging_gate.REJECTED
NO_VERDICT = staging_gate.NO_VERDICT

# Outcome names. These are about WHOSE fault a non-PASS is, which is the only distinction the
# entry condition cares about — a gate that blocks good changes is a regression, a gate that
# correctly rejects a bad one is the point.
OK = "pass"
FALSE_FAILURE = "false-failure"
TRUE_FAILURE = "true-failure"
NEEDS_TRIAGE = "needs-triage"


@dataclass
class Run:
    sha: str
    subject: str
    tags: str
    rc: int
    outcome: str
    note: str


def run_git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def changed_paths(sha: str) -> list[str]:
    """Paths this commit touched, against its first parent."""
    out = run_git("show", "--pretty=format:", "--name-only", "--first-parent", sha)
    return [line for line in out.splitlines() if line]


def staged_services_at(sha: str) -> set[str]:
    """The services the staging cluster ran at this commit, from its inventory at that commit.

    Empty when the file did not exist yet, which is the whole pre-staging era and exactly the
    set of commits that must not be gated.
    """
    try:
        text = run_git("show", f"{sha}:{STAGE_INVENTORY}")
    except subprocess.CalledProcessError:
        return set()
    doc = yaml.safe_load(text) or {}
    return {
        entry["name"]
        for entry in doc.get("containers_list", []) or []
        if "name" in entry
    }


def gateable_services(sha: str) -> set[str]:
    """The services this commit changed that staging can speak for.

    Uses the deployer's own path mapping rather than a second regex, so a change to what
    counts as a k8s role change reaches this script too. `.k8s` is every changed k8s role;
    `.k8s_deploy` would be the auto-deploy-promoted subset, which is the narrower thing this
    script deliberately does not use — see the module docstring.

    Intersected a second time with what staging ran AT THAT COMMIT, so a change to a role that
    only became staging-capable later is not gated on a cluster that could not have taken it.
    """
    cs = deploy_logic.services_from_changed_paths(changed_paths(sha))
    return set(cs.k8s) & set(staging_gate.STAGING_SERVICES) & staged_services_at(sha)


def classify(rc: int) -> tuple[str, str]:
    """Map a staging_gate.py exit code to an outcome and a note.

    NO_VERDICT is a FALSE failure by definition: it means the gate could not be asked, which
    is never a property of the change. REJECTED cannot be classified automatically — it is
    either the gate misfiring or a genuine defect in that commit — so it is reported as
    needing triage rather than guessed at. Guessing would let a broken gate report itself
    healthy, which is the failure this whole measurement exists to prevent.
    """
    if rc == PASS:
        return OK, ""
    if rc == NO_VERDICT:
        return (
            FALSE_FAILURE,
            "gate could not be asked (transport, prep, staleness, timeout)",
        )
    if rc == REJECTED:
        return (
            NEEDS_TRIAGE,
            "staging rejected the commit — triage as true or false by hand",
        )
    return FALSE_FAILURE, f"unexpected exit {rc} from staging_gate.py"


def clean_streak(runs: list[Run]) -> int:
    """Consecutive runs ending at the NEWEST with no false failure.

    Consecutive rather than a mean: a fix that takes the failure rate from 60% to 5% is not
    ready, and an average over the whole history hides exactly that. A true failure does not
    break the streak — the gate working correctly is not evidence against it.
    """
    streak = 0
    for run in reversed(runs):
        if run.outcome == FALSE_FAILURE:
            break
        streak += 1
    return streak


def summarise(runs: list[Run], required: int) -> tuple[str, list[str]]:
    """The verdict, and the reasons it is not met. Empty reasons means met."""
    counts = {
        name: sum(1 for r in runs if r.outcome == name)
        for name in (OK, FALSE_FAILURE, TRUE_FAILURE, NEEDS_TRIAGE)
    }
    reasons = []
    if counts[NEEDS_TRIAGE]:
        reasons.append(
            f"{counts[NEEDS_TRIAGE]} run(s) need triage — a REJECTED is not automatically "
            f"a false failure, and the condition cannot be judged until each is attributed"
        )
    streak = clean_streak(runs)
    if streak < required:
        reasons.append(
            f"longest clean streak ending at the newest run is {streak}, need {required}"
        )
    verdict = "MET" if not reasons else "NOT MET"
    return verdict, reasons


def collect(ref: str, count: int) -> list[tuple[str, str, set[str]]]:
    """The oldest-to-newest list of (sha, subject, services) this script would gate."""
    log = run_git("log", "--format=%H\t%s", "-400", ref)
    found = []
    for line in log.splitlines():
        sha, _, subject = line.partition("\t")
        services = gateable_services(sha)
        if services:
            found.append((sha, subject, services))
        if len(found) >= count:
            break
    return list(reversed(found))


def staging_head() -> str | None:
    """The gate checkout's HEAD, or None if it cannot be read.

    Read rather than assumed: the gate's tree only fast-forwards, so any planned commit that is
    already an ancestor of this is unrunnable — the remote script's HEAD-equals-SHA assert will
    refuse it, and the run would score a false failure that says nothing about the gate.
    """
    completed = subprocess.run(
        ["ssh", STAGE_HOST, f"cd {STAGE_REPO} && git rev-parse HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.stdout.strip() or None if completed.returncode == 0 else None


def is_ancestor(sha: str, of: str) -> bool:
    return (
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", sha, of],
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )


def unrunnable(
    plan: list[tuple[str, str, set[str]]], head: str, ancestor_check=is_ancestor
) -> list[str]:
    """The planned SHAs the gate's checkout has already moved past."""
    return [sha for sha, _, _ in plan if ancestor_check(sha, head)]


def load_ledger(path: Path) -> list[Run]:
    """Runs recorded by earlier invocations, oldest first.

    The condition is a streak over the gate's whole measured history, not over one invocation:
    a backfill is a one-shot (the tree ends it at the newest planned commit), so without this
    every future gated commit would start counting from zero.
    """
    if not path.exists():
        return []
    runs = []
    for line in path.read_text().splitlines():
        if line.strip():
            runs.append(Run(**json.loads(line)))
    return runs


def gate(sha: str, tags: str, timeout: int) -> int:
    return subprocess.run(
        [
            sys.executable,
            str(_HERE / "staging_gate.py"),
            sha,
            "--tags",
            tags,
            "--timeout",
            str(timeout),
        ],
        check=False,
    ).returncode


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--count", type=int, default=20, help="runs to collect (default 20)"
    )
    ap.add_argument(
        "--from", dest="ref", default="origin/master", help="ref to walk back from"
    )
    ap.add_argument(
        "--timeout", type=int, default=1800, help="per-run timeout, seconds"
    )
    ap.add_argument(
        "--jsonl", type=Path, help="append one JSON record per run to this file"
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="list the commits and tags that would be gated, and run nothing",
    )
    ap.add_argument(
        "--required",
        type=int,
        default=20,
        help="clean streak the entry condition needs (default 20). Separate from --count "
        "because the streak spans the ledger, not one invocation.",
    )
    ap.add_argument(
        "--allow-ancestors",
        action="store_true",
        help="plan commits the gate's checkout has already moved past (they will fail prep)",
    )
    args = ap.parse_args()

    plan = collect(args.ref, args.count)
    if not plan:
        print(
            f"no commit in the last 400 of {args.ref} touches a staging-subset service"
        )
        return 1
    print(f"{len(plan)} gateable commit(s), oldest first:")
    for sha, subject, services in plan:
        print(f"  {sha[:8]}  {','.join(sorted(services)):<40}  {subject[:60]}")
    head = staging_head()
    if head is None:
        print(f"warning: cannot read {STAGE_HOST}:{STAGE_REPO} HEAD — planning blind")
    else:
        stale = unrunnable(plan, head)
        if stale and not args.allow_ancestors:
            print(
                f"\n{len(stale)} of {len(plan)} planned commit(s) are ancestors of the gate's "
                f"checkout at {head[:8]}, which only fast-forwards. Each would fail prep and "
                f"score a false failure that says nothing about the gate.\n"
                f"To run this window, reset that checkout behind the oldest one first:\n"
                f"  ssh {STAGE_HOST} 'cd {STAGE_REPO} && git reset --hard {plan[0][0][:8]}~1'\n"
                f"Or narrow the window: --from {head[:8]}..origin/master"
            )
            return 1
    if args.dry_run:
        return 0

    runs: list[Run] = load_ledger(args.jsonl) if args.jsonl else []
    if runs:
        print(f"{len(runs)} run(s) carried over from {args.jsonl}")
    for index, (sha, subject, services) in enumerate(plan, start=1):
        tags = ",".join(sorted(services))
        print(f"\n=== {index}/{len(plan)}  {sha[:8]}  --tags {tags} ===", flush=True)
        rc = gate(sha, tags, args.timeout)
        outcome, note = classify(rc)
        record = Run(
            sha=sha, subject=subject, tags=tags, rc=rc, outcome=outcome, note=note
        )
        runs.append(record)
        print(f"--> {outcome}{(' — ' + note) if note else ''}", flush=True)
        if args.jsonl:
            with args.jsonl.open("a") as handle:
                handle.write(json.dumps(asdict(record)) + "\n")

    verdict, reasons = summarise(runs, args.required)
    print(f"\n{'=' * 72}\nentry-condition part 1: {verdict}")
    print(f"  runs={len(runs)}  clean streak={clean_streak(runs)}/{args.required}")
    for name in (OK, FALSE_FAILURE, TRUE_FAILURE, NEEDS_TRIAGE):
        print(f"  {name:<14} {sum(1 for r in runs if r.outcome == name)}")
    for reason in reasons:
        print(f"  ! {reason}")
    return 0 if verdict == "MET" else 1


if __name__ == "__main__":
    raise SystemExit(main())
