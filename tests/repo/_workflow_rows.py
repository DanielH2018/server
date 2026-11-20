"""The workflow-row table: census rows over the GitHub Actions workflows and actions.

`test_census_rows_workflows.py` runs every row here and in the `_*_workflow_rows.py`
modules. The rows below read a workflow as text; a row that reads one step or job property
parses it with `judge`, and proves its red half on the real file with one property broken,
built by `mutant`.
"""

import copy
import re
from collections.abc import Callable
from functools import cache
from pathlib import Path

import yaml
from _helpers import REPO
from _row_table import Census, Offence, Subject, lines_matching, tracked
from lib import yaml_fast

PROMOTE = ".github/workflows/promote.yml"
CI = ".github/workflows/ci.yml"
SETUP_ACTION = ".github/actions/setup/action.yml"
_DUMPER = getattr(yaml, "CSafeDumper", yaml.SafeDumper)

Workflow = dict
Judge = Callable[[Workflow], list[str]]


@cache
def _parse(text: str) -> Workflow:
    return yaml_fast.safe_load(text) or {}


def parsed(subject: Subject) -> Workflow:
    """`subject` parsed, shared between rows: a judge reads it and never mutates it."""
    return _parse(subject.text)


def load(rel: str | Path) -> Workflow:
    """A tracked workflow or action, parsed into a copy the caller owns."""
    return copy.deepcopy(_parse((REPO / rel).read_text(encoding="utf-8")))


def judge(rule: Judge) -> Offence:
    """An offence that runs `rule` on the parsed document: one line per broken property."""
    return lambda subject: rule(parsed(subject))


class Mutant(Subject):
    """A tracked workflow with one property broken by `mutate`, for a row's `red` half.

    Its `rel` names the source and the case, so a red that passes says which case. Reading
    its text raises when `mutate` changed nothing, since an unchanged file proves nothing.
    """

    def __init__(self, source: str, case: str, mutate: Callable[[Workflow], object]):
        super().__init__(f"{source} [{case}]")
        self._source = source
        self._mutate = mutate

    @property
    def text(self) -> str:
        if self._text is None:
            doc = load(self._source)
            self._mutate(doc)
            if doc == load(self._source):
                raise AssertionError(
                    f"{self.rel}: the mutation left the file unchanged"
                )
            self._text = yaml.dump(doc, Dumper=_DUMPER, sort_keys=False)
        return self._text


def mutants(source: str, **cases: Callable[[Workflow], object]) -> tuple[Mutant, ...]:
    """One `Mutant` of `source` per case, named by its keyword with `_` read as a space."""
    return tuple(
        Mutant(source, case.replace("_", " "), mutate) for case, mutate in cases.items()
    )


def triggers(wf: Workflow) -> dict:
    # YAML 1.1 reads the bare key `on` as the boolean True.
    return wf.get("on", wf.get(True)) or {}


def jobs(wf: Workflow) -> dict[str, dict]:
    return wf.get("jobs") or {}


def steps(job: dict) -> list[dict]:
    return job.get("steps") or []


def uses(step: dict, action: str) -> bool:
    return str(step.get("uses", "")).split("@")[0] == action


def runs(step: dict) -> str:
    """A step's `run` script, or "" for a step that uses an action."""
    return str(step.get("run", ""))


def checkouts(job: dict) -> list[dict]:
    return [s for s in steps(job) if uses(s, "actions/checkout")]


def step_index(job: dict, step_id: str) -> int | None:
    return next((i for i, s in enumerate(steps(job)) if s.get("id") == step_id), None)


def index_of(job: dict, step_id: str) -> int:
    """`step_index` for a mutation, which raises when the job has no step `step_id`."""
    at = step_index(job, step_id)
    if at is None:
        raise KeyError(f"no step with id {step_id!r}")
    return at


def condition(node: dict) -> str:
    """A job's or a step's `if`, with its whitespace collapsed to single spaces."""
    return " ".join(str(node.get("if", "")).split())


_RUNS_ON = re.compile(r"^\s*runs-on:\s*(.+?)\s*$", re.MULTILINE)
_VERSIONED = re.compile(r"^[a-z]+-\d+(\.\d+)?(-[a-z]+)?$")
_PACKAGE_BUILD_JOB = re.compile(
    r"^  host_packages_build:\n(.*?)^  host_packages:", re.MULTILINE | re.DOTALL
)
_CURL_STEP = re.compile(r"^\s*(?:run:\s*)?curl\b.*$", re.MULTILINE)
_CURL_RETRIES = re.compile(r"--retry (\d+)\b.*--retry-delay (\d+)\b")
# The span of the 504s in run 42054080101.
_RELEASE_OUTAGE_S = 300
_TAG_MOVE = re.compile(r"\bflux tag artifact\b")
_BY_DIGEST = re.compile(r'flux tag artifact\s+"?oci://[^\s"@]+@')


def _package_build_download_offences(text: str) -> list[str]:
    job = _PACKAGE_BUILD_JOB.search(text)
    curls = _CURL_STEP.findall(job.group(1)) if job else []
    if not curls:
        return ["host_packages_build holds no curl download"]
    offences = []
    for line in curls:
        retries = _CURL_RETRIES.search(line)
        if not retries or int(retries[1]) * int(retries[2]) < _RELEASE_OUTAGE_S:
            offences.append(line.strip())
    return offences


_BARE_PYTHON = re.compile(r"(?<![\w/.-])python3?\s+(?:-\S+\s+)*[\"']?scripts/\S+\.py")


def _bare_python_without_root(wf: Workflow) -> list[str]:
    offences = []
    for job_name, job in jobs(wf).items():
        for step in steps(job):
            if any("PYTHONPATH" in (node.get("env") or {}) for node in (wf, job, step)):
                continue
            for line in runs(step).splitlines():
                call = _BARE_PYTHON.search(line)
                if call and not re.search(r"\buv\b", line[: call.start()]):
                    offences.append(f"{job_name}: {step.get('name', '?')}: {call[0]}")
    return offences


def _drop_step_pythonpath(job: str, step: str) -> Callable[[Workflow], None]:
    return lambda wf: next(
        s for s in wf["jobs"][job]["steps"] if s.get("name") == step
    )["env"].pop("PYTHONPATH")


WORKFLOW_ROWS = (
    Census(
        name="workflow-runners-are-pinned",
        reason=(
            "`runs-on: ubuntu-latest` floats: GitHub rolls the alias with no commit here, so a "
            "`language: system` prek hook can change verdict between two runs of one SHA. A "
            "versioned label (`ubuntu-24.04`) moves only through a Renovate PR, which runs the "
            "hooks on the new image before merge (docs/adr/0018)."
        ),
        files=lambda: tracked(".github/workflows/*.yml"),
        offence=lambda s: [
            f"floating runner {label!r}"
            for label in _RUNS_ON.findall(s.text)
            if not _VERSIONED.match(label)
        ],
        red=(
            Subject("a.yml", "jobs:\n  a:\n    runs-on: ubuntu-latest\n"),
            Subject("b.yml", "jobs:\n  b:\n    runs-on: ${{ env.RUNNER }}\n"),
            Subject("c.yml", "jobs:\n  c:\n    runs-on: self-hosted\n"),
        ),
        green=(Subject("a.yml", "jobs:\n  a:\n    runs-on: ubuntu-24.04\n"),),
        must_find=frozenset(
            f".github/workflows/{name}"
            for name in ("ci.yml", "image-smoke.yml", "renovate-config-canary.yml")
        ),
    ),
    Census(
        name="workflows-fetch-through-the-shared-script",
        reason=(
            "The merge-base fetch needs a token in a one-shot `extraheader`, because every "
            "checkout runs with persist-credentials: false. `scripts/dev/pr_changed_files.sh` "
            "holds that fetch once, and image-smoke calls it even though it reads none of its "
            "file lists (#3777). A workflow that inlines the header again "
            "is a copy the next change to the fetch has to find by hand."
        ),
        files=lambda: tracked(".github/workflows/*.yml"),
        offence=lines_matching(re.compile(r"extraheader")),
        red=(
            Subject(
                "a.yml",
                'run: |\n  git -c "http.https://github.com/.extraheader=AUTHORIZATION: x" '
                "fetch origin main\n",
            ),
        ),
        green=(
            Subject(
                "a.yml",
                'run: |\n  "$GITHUB_WORKSPACE/scripts/dev/pr_changed_files.sh"\n',
            ),
        ),
        must_find=frozenset(
            f".github/workflows/{name}" for name in ("ci.yml", "image-smoke.yml")
        ),
    ),
    Census(
        name="workflow-downloads-retry-every-error",
        reason=(
            "`curl --retry` alone retries only 408, 429 and 5xx, so a 403 from GitHub's "
            "release CDN fails the step on its first answer. That 403 turned master red in "
            "run 40160070856 with no change to the code. `--retry-all-errors` retries it, and "
            "the sha256 check after each download still refuses a wrong file. `-L` is the "
            "download shape: promote's registry probes use `-fsS` and read the status code "
            "themselves, so a 404 there is an answer and must not be retried."
        ),
        files=lambda: tracked(
            ".github/workflows/*.yml", ".github/actions/*/action.yml"
        ),
        offence=lines_matching(
            re.compile(r"\bcurl\b(?!.*--retry-all-errors).*\s-fsSL")
        ),
        red=(
            Subject("a.yml", 'run: curl --retry 3 -fsSLo "$RUNNER_TEMP/bin/sops" \\\n'),
            Subject("b.yml", "run: curl -fsSL -o /tmp/vale.tar.gz \\\n"),
        ),
        green=(
            Subject(
                "a.yml",
                "run: curl --retry 5 --retry-all-errors --retry-delay 5 -fsSLo x \\\n",
            ),
            Subject("b.yml", "  | curl --retry 3 -fsS -K - -o out -w '%{http_code}'\n"),
        ),
        must_find=frozenset(
            {".github/workflows/ci.yml", ".github/workflows/promote.yml"}
        ),
    ),
    Census(
        name="workflow-tag-moves-name-the-digest",
        reason=(
            "`decide` verifies a digest, and `prod`, `superseding`, `last-good` and `held` "
            "must land on that digest. A move by the mutable `sha-<T>` tag lands on whatever "
            "`sha-<T>` names at the moment of the move, which nothing re-checks against the "
            "verified digest (#8223). `oci://<repo>@<digest>` names the bytes decide verified."
        ),
        files=lambda: tracked(".github/workflows/*.yml"),
        offence=lambda s: [
            f"line {n}: {line.strip()}"
            for n, line in enumerate(s.text.splitlines(), 1)
            if _TAG_MOVE.search(line) and not _BY_DIGEST.search(line)
        ],
        count=lambda s: len(_TAG_MOVE.findall(s.text)),
        min_matches=10,
        red=(
            Subject(
                "a.yml",
                'run: flux tag artifact "oci://$repo:sha-$TARGET" --tag prod\n',
            ),
            Subject(
                "b.yml",
                "run: flux tag artifact oci://ghcr.io/x/y:sha-abc --tag held\n",
            ),
            Subject("c.yml", 'run: flux tag artifact "$artifact" --tag prod\n'),
            Subject(
                "d.yml", "run: |\n  flux tag artifact \\\n    oci://x@y --tag prod\n"
            ),
        ),
        green=(
            Subject(
                "a.yml",
                'run: flux tag artifact "oci://$repo@$DIGEST" --tag prod\n',
            ),
        ),
        must_find=frozenset({".github/workflows/promote.yml"}),
    ),
    Census(
        name="package-build-downloads-outlast-a-release-outage",
        reason=(
            "GitHub's release host answered 504 for about five minutes in run 42054080101. A "
            "download in `host_packages_build` with a shorter retry window fails the job there, "
            "and land.sh reads that as a red landing of the PR under test. "
            "`hosts/lib/homelab_tool_fetch.py:TOOL_RETRY_WINDOW_S` covers the pinned tools."
        ),
        files=lambda: tracked(".github/workflows/ci.yml"),
        offence=lambda s: _package_build_download_offences(s.text),
        red=(
            Subject(
                "a.yml",
                "  host_packages_build:\n"
                "          curl --retry 5 --retry-all-errors --retry-delay 5 -fsSLo x \\\n"
                "  host_packages:\n",
            ),
            Subject("b.yml", "  host_packages_build:\n  host_packages:\n"),
        ),
        green=(
            Subject(
                "a.yml",
                "  host_packages_build:\n"
                "          curl --retry 12 --retry-all-errors --retry-delay 30 -fsSLo x \\\n"
                "  host_packages:\n",
            ),
        ),
        must_find=frozenset({".github/workflows/ci.yml"}),
    ),
    Census(
        name="bare-python-steps-supply-the-scripts-root",
        reason=(
            "`scripts/` is the import root (docs/adr/0039-scripts-is-installed-in-place.md), "
            "and only the venv's `.pth` or `PYTHONPATH` supplies it. A step that runs "
            "`python3 scripts/...` with no venv gets neither, so a module with no `sys.path` "
            "insert of `scripts/` fails its first `from lib` import. `renovate_extract.py` did "
            "that in PR #9417's CI. A step that calls through `uv run` reads the `.pth`."
        ),
        files=lambda: tracked(".github/workflows/*.yml"),
        offence=judge(_bare_python_without_root),
        red=mutants(
            CI,
            the_renovate_scope_step_without_its_root=_drop_step_pythonpath(
                "renovate-config", "Decide which Renovate checks this PR needs"
            ),
        )
        + (
            Subject(
                "a.yml",
                "jobs:\n  a:\n    steps:\n      - run: python3 -I scripts/dev/x.py\n",
            ),
        ),
        green=(
            Subject(
                "a.yml",
                "jobs:\n  a:\n    steps:\n      - run: uv run --frozen python scripts/dev/x.py\n"
                "      - env:\n          PYTHONPATH: scripts\n"
                "        run: python3 scripts/dev/x.py\n",
            ),
        ),
        must_find=frozenset(
            f".github/workflows/{name}"
            for name in (
                "ci.yml",
                "image-smoke.yml",
                "promote.yml",
                "pytest-sweep.yml",
                "session-milestone.yml",
            )
        ),
    ),
)
