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

import deploy_logic  # noqa: E402
import staging_gate  # noqa: E402

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


def gateable_services(sha: str) -> set[str]:
    """The services this commit changed that staging can speak for.

    Uses the deployer's own path mapping rather than a second regex, so a change to what
    counts as a k8s role change reaches this script too. `.k8s` is every changed k8s role;
    `.k8s_deploy` would be the auto-deploy-promoted subset, which is the narrower thing this
    script deliberately does not use — see the module docstring.
    """
    cs = deploy_logic.services_from_changed_paths(changed_paths(sha))
    return set(cs.k8s) & set(staging_gate.STAGING_SERVICES)


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
    if args.dry_run:
        return 0

    runs: list[Run] = []
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

    verdict, reasons = summarise(runs, args.count)
    print(f"\n{'=' * 72}\nentry-condition part 1: {verdict}")
    print(f"  runs={len(runs)}  clean streak={clean_streak(runs)}/{args.count}")
    for name in (OK, FALSE_FAILURE, TRUE_FAILURE, NEEDS_TRIAGE):
        print(f"  {name:<14} {sum(1 for r in runs if r.outcome == name)}")
    for reason in reasons:
        print(f"  ! {reason}")
    return 0 if verdict == "MET" else 1


if __name__ == "__main__":
    raise SystemExit(main())
