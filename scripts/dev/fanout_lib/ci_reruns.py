"""What a fan-out review record says about the CI gate's re-run of failed jobs (#7689).

The CI gate re-runs a PR's failed jobs after a resume that pushed nothing, and records
`ci_rerun` (#7674). `ci_gate` then reads `passed ...`, `failed ...: <jobs>` or `no verdict ...`,
so the re-run's outcome is that leading word. The record names the failed jobs only when the
re-run did not clear them, so a flake that passed has no job names. A record written before
`ci_rerun` existed has no key and reads as no re-run.
"""

NO_RERUN = {"ci_rerun": False, "ci_rerun_outcome": "", "ci_rerun_jobs": []}


def rerun_fields(record: dict) -> dict:
    """Whether the gate re-ran the failed jobs, how that ended, and the jobs still red."""
    if not record.get("ci_rerun"):
        return NO_RERUN
    gate = str(record.get("ci_gate") or "")
    outcome = next(
        (w for w in ("passed", "failed") if gate.startswith(w)), "no verdict"
    )
    jobs = gate.partition(": ")[2] if outcome == "failed" else ""
    return {
        "ci_rerun": True,
        "ci_rerun_outcome": outcome,
        "ci_rerun_jobs": jobs.split(", ") if jobs else [],
    }


def rerun_line(rows: list[dict]) -> str:
    """The summary line: re-runs, how many passed, and the jobs that stayed red through one."""
    reran = [r for r in rows if r.get("ci_rerun")]
    passed = sum(1 for r in reran if r.get("ci_rerun_outcome") == "passed")
    jobs = sorted({j for r in reran for j in r.get("ci_rerun_jobs") or []})
    return (
        f"CI re-runs: {len(reran)} of {len(rows)} PRs, {passed} passed"
        f", failed jobs: {', '.join(jobs) or '-'}"
    )
