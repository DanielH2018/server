"""The census row holding that some step outside the CI verdict runs the k3s-downloading cases."""

from collections.abc import Callable
from pathlib import Path

from _ci_workflow_rows import CASES_ENTRY, GATE_JOB, LEGS_JOB, ci_job
from _row_table import Census, Subject, tracked
from _workflow_rows import Workflow, jobs, load, runs, steps


class EveryWorkflow(Subject):
    """Every tracked workflow, parsed: one subject for a claim that spans the files."""

    def __init__(
        self, rel: str, mutate: Callable[[dict[str, Workflow]], object] | None = None
    ):
        super().__init__(rel)
        self._mutate = mutate
        self._docs: dict[str, Workflow] | None = None

    @staticmethod
    def _load() -> dict[str, Workflow]:
        return {Path(rel).name: load(rel) for rel in tracked(".github/workflows/*.yml")}

    @property
    def docs(self) -> dict[str, Workflow]:
        if self._docs is None:
            self._docs = self._load()
            if self._mutate:
                self._mutate(self._docs)
                if self._docs == self._load():
                    raise AssertionError(f"{self.rel}: the mutation changed nothing")
        return self._docs


def _case_invocations(docs: dict[str, Workflow]) -> list[tuple[str, str, bool]]:
    """Each step that runs the cases: (workflow, job id, whether it gates the PR)."""
    found = []
    for name, wf in sorted(docs.items()):
        gate_needs = set(ci_job(wf, GATE_JOB).get("needs") or ())
        for job_id, job in jobs(wf).items():
            for step in steps(job):
                if CASES_ENTRY in runs(step):
                    gates = (
                        name == "ci.yml"
                        and job_id in gate_needs
                        and step.get("continue-on-error") is not True
                    )
                    found.append((name, job_id, gates))
    return found


def _k3s_leg_problems(subject: Subject) -> list[str]:
    assert isinstance(subject, EveryWorkflow)
    invocations = _case_invocations(subject.docs)
    found = []
    if ("ci.yml", LEGS_JOB) not in {(w, j) for w, j, gates in invocations if gates}:
        found.append(f"no gating step runs {CASES_ENTRY}: {invocations}")
    if all(gates for _, _, gates in invocations):
        found.append(
            "every step that runs the cases gates the PR, so none can leave k3s unseeded "
            f"without a release outage failing the run: {invocations}"
        )
    return found


def _make_k3s_leg_gate(wf: dict[str, Workflow]) -> None:
    ci = wf["ci.yml"]
    ci["jobs"][GATE_JOB]["needs"].append("k3s_download")
    for step in steps(ci["jobs"]["k3s_download"]):
        step.pop("continue-on-error", None)


def _stop_the_legs_gating(wf: dict[str, Workflow]) -> None:
    for step in steps(wf["ci.yml"]["jobs"][LEGS_JOB]):
        if CASES_ENTRY in runs(step):
            step["continue-on-error"] = True


def _case_count(subject: Subject) -> int:
    assert isinstance(subject, EveryWorkflow)
    return len(_case_invocations(subject.docs))


K3S_DOWNLOAD_LEG_DOES_NOT_GATE = Census(
    name="ci-a-non-gating-step-runs-the-cases-that-download-k3s",
    reason=(
        "The gating `host_packages` legs seed the k3s binary, so they never exercise "
        "install.sh's own download. The leg that does must sit outside the verdict: a "
        "step marked `continue-on-error`, a job the `prek` gate does not need, or a "
        "workflow of its own. `hosts/lib/ci_verdict.py` reads the check-runs of the "
        "gate's `needs`, so a job-level `continue-on-error` inside them still gates. The "
        "claim spans every workflow, so the row's subject is all of them."
    ),
    files=lambda: [EveryWorkflow("every .github/workflows/*.yml")],
    offence=_k3s_leg_problems,
    count=_case_count,
    red=(
        EveryWorkflow("the k3s leg gating", _make_k3s_leg_gate),
        EveryWorkflow("the legs not gating", _stop_the_legs_gating),
    ),
    green=(EveryWorkflow("every .github/workflows/*.yml"),),
    must_find=frozenset({"every .github/workflows/*.yml"}),
)
