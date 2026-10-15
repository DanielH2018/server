"""A PR that changes only the container cases' inputs runs those cases, on both legs (#5004).

`host_packages` in ci.yml runs its container cases (H3.3b) when step 2's render diff reports
`changed=true`, or on a dispatch. The cases' own inputs are not render inputs: the test image's
Dockerfile, the fixture specs under `hosts/lib/tests/container/fixture/`, and the scripts that
drive the cases. A PR touching only those changes no render, so its run skipped the cases and
merged green without them.

HOW THE JOB IS SIMULATED. Each test builds a PR merge ref in a scratch clone of this repository
that commits one edit to one input. It then walks the job's steps in order, up to the cases step:

- A step with both an `id:` and a `run:` whose `if:` holds is RUN, as the runner would run it.
  The clone is its working directory and its `$GITHUB_WORKSPACE`. Its own `env:` block is
  resolved against the simulated contexts, and what it writes to `$GITHUB_OUTPUT` becomes
  `steps.<id>.outputs`. The render step runs for real, so its `changed` output is the real
  render diff. `uv` runs against this test's own environment with syncing off.
- Every other step is not run: the nfpm fetch, the builds and the cases themselves.

The test then evaluates the `if:` of every step the cases depend on, once per matrix leg.
`uses:` steps produce no outputs here, so a trigger computed by an action is invisible to the
simulation. The evaluator raises on an expression form it does not implement rather than
reading it as false.

Run: uv run pytest ansible/tests/repo/test_host_package_cases_run_on_their_inputs.py
"""

import re
import sys
from pathlib import Path

import pytest
from _helpers import REPO
from lib import yaml_fast
from lib.git_testing import git, git_out, scrubbed_env
from lib.proc_testing import run

CI_WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
JOB = "host_packages"

CASES_STEP = "Run the updater cases in systemd containers"
# The steps the cases read from. `host_package_cases.py` globs each side's updater .deb out of
# the build step's output and boots the image the image step tags, so a run that skips either
# fails the cases rather than running them.
CASE_PREREQUISITES = (
    "Build both renders with the pinned nfpm",
    "Build the test image for this release",
)

DOCKERFILE = "hosts/lib/tests/container/Dockerfile"
# The inputs #5004 names, each by a concrete member. The nested fixture file catches a trigger
# that matches exact names where it should match the directory.
NAMED_INPUTS = (
    "hosts/lib/tests/container/fixture/base.yml",
    "hosts/lib/tests/container/fixture/units/homelab-fixture-kept.service",
    "scripts/validate/host_package_cases.py",
    "scripts/validate/validate_lib/host_fixtures.py",
    "scripts/validate/validate_lib/host_container.py",
)
UNRELATED = "README.md"


def _tracked_container_inputs() -> list[str]:
    """Every tracked file under the image's build context, which is the census half."""
    out = git_out(REPO, "ls-files", "hosts/lib/tests/container")
    files = out.splitlines()
    assert DOCKERFILE in files, f"git ls-files lost {DOCKERFILE}: {files}"
    return files


def _case_inputs() -> list[str]:
    paths = dict.fromkeys([*NAMED_INPUTS, *_tracked_container_inputs()])
    paths.pop(DOCKERFILE, None)
    return list(paths)


# A small GitHub Actions expression evaluator: literals, context paths, `! == != && ||`,
# parentheses and the status functions.

_TOKEN = re.compile(
    r"\s*(?:(?P<str>'(?:[^']|'')*')|(?P<num>\d+(?:\.\d+)?)"
    r"|(?P<op>==|!=|&&|\|\||!|\(|\)|,)|(?P<name>[A-Za-z_][A-Za-z0-9_.\-*]*))"
)
_STATUS = {"always": True, "success": True, "failure": False, "cancelled": False}


def _tokens(text: str) -> list[tuple[str, str]]:
    tokens, pos = [], 0
    text = text.strip()
    while pos < len(text):
        match = _TOKEN.match(text, pos)
        if not match or match.end() == pos:
            raise ValueError(f"cannot tokenise {text[pos:]!r} in {text!r}")
        kind = match.lastgroup
        assert kind is not None
        tokens.append((kind, match.group(kind)))
        pos = match.end()
    return tokens


def _truthy(value: object) -> bool:
    return value not in (None, False, "", 0)


def evaluate(expression: str, context: dict) -> object:
    """Evaluate a GitHub expression against `context`; raise on any form outside the subset."""
    tokens = _tokens(expression)
    pos = 0

    def peek() -> tuple[str, str] | None:
        return tokens[pos] if pos < len(tokens) else None

    def take() -> tuple[str, str]:
        nonlocal pos
        pos += 1
        return tokens[pos - 1]

    def lookup(path: str) -> object:
        root, *rest = path.split(".")
        if root not in context:
            raise ValueError(f"unknown context {root!r} in {expression!r}")
        value: object = context[root]
        for key in rest:
            value = value.get(key, "") if isinstance(value, dict) else ""
        return value

    def primary() -> object:
        kind, text = take()
        if kind == "op" and text == "!":
            return not _truthy(primary())
        if kind == "op" and text == "(":
            value = disjunction()
            assert take() == ("op", ")"), expression
            return value
        if kind == "str":
            return text[1:-1].replace("''", "'")
        if kind == "num":
            return float(text)
        if kind == "name":
            if text in ("true", "false"):
                return text == "true"
            if text == "null":
                return None
            if peek() == ("op", "("):
                take()
                assert take() == ("op", ")"), f"function arguments in {expression!r}"
                if text not in _STATUS:
                    raise ValueError(f"unsupported function {text}() in {expression!r}")
                return _STATUS[text]
            return lookup(text)
        raise ValueError(f"unexpected {text!r} in {expression!r}")

    def comparison() -> object:
        left = primary()
        while peek() in (("op", "=="), ("op", "!=")):
            _, op = take()
            right = primary()
            same = str(left).lower() == str(right).lower()
            left = same if op == "==" else not same
        return left

    def conjunction() -> object:
        left = comparison()
        while peek() == ("op", "&&"):
            take()
            right = comparison()
            left = right if _truthy(left) else left
        return left

    def disjunction() -> object:
        left = conjunction()
        while peek() == ("op", "||"):
            take()
            right = conjunction()
            left = left if _truthy(left) else right
        return left

    value = disjunction()
    if pos != len(tokens):
        raise ValueError(f"trailing tokens in {expression!r}")
    return value


def _condition(step: dict, context: dict) -> bool:
    if "if" not in step:
        return True
    text = str(step["if"]).strip()
    if text.startswith("${{") and text.endswith("}}"):
        text = text[3:-2]
    return _truthy(evaluate(text, context))


def _interpolate(value: object, context: dict) -> str:
    def one(match: re.Match[str]) -> str:
        result = evaluate(match.group(1), context)
        if isinstance(result, bool):
            return "true" if result else "false"
        return "" if result is None else str(result)

    return re.sub(r"\$\{\{(.*?)\}\}", one, str(value))


def _read_outputs(text: str) -> dict[str, str]:
    """`$GITHUB_OUTPUT` as the runner parses it: `k=v` lines and `k<<DELIM` blocks."""
    outputs: dict[str, str] = {}
    lines = iter(text.splitlines())
    for line in lines:
        if "<<" in line and ("=" not in line or line.index("<<") < line.index("=")):
            key, delimiter = line.split("<<", 1)
            body = []
            for inner in lines:
                if inner == delimiter:
                    break
                body.append(inner)
            outputs[key] = "\n".join(body)
        elif "=" in line:
            key, value = line.split("=", 1)
            outputs[key] = value
    return outputs


@pytest.fixture(scope="module")
def clone(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A clone of this repository's HEAD, sharing its object store.

    `--shared` reads objects through an alternate, so HEAD resolves even when it is a detached
    merge ref that no branch carries, as it is in CI's pytest job."""
    repo = tmp_path_factory.mktemp("host-cases") / "clone"
    head = git_out(REPO, "rev-parse", "HEAD")
    git(REPO.parent, "clone", "-q", "--shared", "--no-checkout", str(REPO), str(repo))
    git(repo, "checkout", "-q", "--detach", head)
    git(repo, "branch", "-f", "master", head)
    return repo


def _pr_merge_ref(clone: Path, path: str) -> None:
    """Stand where a pull_request run stands: master merged with a PR editing only `path`."""
    branch = "pr-" + re.sub(r"[^A-Za-z0-9]+", "-", path)
    git(clone, "checkout", "-q", "--detach", "master")
    git(clone, "checkout", "-q", "-B", branch)
    target = clone / path
    target.write_text(target.read_text() + "\n# touched by the #5004 test\n")
    git(clone, "commit", "-q", "-am", f"edit {path}")
    git(clone, "checkout", "-q", "--detach", "master")
    git(clone, "merge", "-q", "--no-ff", "--no-edit", branch)


def _simulate(clone: Path, tmp_path: Path, leg: dict) -> tuple[dict[str, bool], dict]:
    """Run the job's output-producing steps on one leg; return each step's verdict and the context."""
    workflow = yaml_fast.safe_load(CI_WORKFLOW.read_text())
    job = workflow["jobs"][JOB]
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir(parents=True)
    context: dict = {
        "github": {
            "event_name": "pull_request",
            "base_ref": "master",
            "head_ref": "pr",
            "ref": "refs/pull/1/merge",
            "sha": git_out(clone, "rev-parse", "HEAD"),
            "workspace": str(clone),
        },
        "matrix": leg,
        "steps": {},
        "env": {},
        "runner": {"temp": str(runner_temp), "os": "Linux"},
        "secrets": {},
        "vars": {},
        "inputs": {},
    }
    for scope in (workflow.get("env") or {}, job.get("env") or {}):
        context["env"].update({k: _interpolate(v, context) for k, v in scope.items()})

    verdicts: dict[str, bool] = {}
    for index, step in enumerate(job["steps"]):
        name = step.get("name", step.get("uses", f"step {index}"))
        holds = _condition(step, context)
        verdicts[name] = holds
        if name == CASES_STEP:
            context["cases_env"] = {
                k: _interpolate(v, context) for k, v in (step.get("env") or {}).items()
            }
            return verdicts, context
        if not (holds and "id" in step and "run" in step):
            continue
        output = runner_temp / f"output-{index}"
        output.write_text("")
        env = scrubbed_env(
            **context["env"],
            **{k: _interpolate(v, context) for k, v in (step.get("env") or {}).items()},
            RUNNER_TEMP=str(runner_temp),
            GITHUB_OUTPUT=str(output),
            GITHUB_ENV=str(runner_temp / "env"),
            GITHUB_PATH=str(runner_temp / "path"),
            GITHUB_STEP_SUMMARY=str(runner_temp / "summary.md"),
            GITHUB_EVENT_NAME="pull_request",
            GITHUB_BASE_REF="master",
            GITHUB_WORKSPACE=str(clone),
            UV_PROJECT_ENVIRONMENT=sys.prefix,
            UV_NO_SYNC="1",
        )
        script = _interpolate(step["run"], context)
        proc = run(
            ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
            cwd=clone,
            env=env,
            timeout=300,
        )
        assert proc.returncode == 0, (
            f"step {name!r} failed on the simulated PR:\n{proc.stdout}\n{proc.stderr}"
        )
        context["steps"][step["id"]] = {
            "outputs": _read_outputs(output.read_text()),
            "outcome": "success",
            "conclusion": "success",
        }
    raise AssertionError(f"job {JOB} has no step named {CASES_STEP!r}")


def _legs() -> list[dict]:
    job = yaml_fast.safe_load(CI_WORKFLOW.read_text())["jobs"][JOB]
    legs = job["strategy"]["matrix"]["include"]
    assert len(legs) >= 2, f"{JOB} lost a matrix leg: {legs}"
    return legs


def _runs_per_leg(
    clone: Path, tmp_path: Path, path: str
) -> list[tuple[dict, dict[str, bool], dict]]:
    _pr_merge_ref(clone, path)
    results = []
    for number, leg in enumerate(_legs()):
        verdicts, context = _simulate(clone, tmp_path / f"leg-{number}", leg)
        results.append((leg, verdicts, context))
    return results


def _assert_cases_run(clone: Path, tmp_path: Path, path: str) -> None:
    for leg, verdicts, context in _runs_per_leg(clone, tmp_path, path):
        render = context["steps"].get("render", {}).get("outputs", {})
        assert verdicts[CASES_STEP], (
            f"a PR touching only {path} skips {CASES_STEP!r} on leg {leg} "
            f"(render outputs {render}), so it merges green without the cases it changed"
        )
        assert context["cases_env"].get("UPGRADE") == "true", (
            f"the cases step runs on leg {leg} but stands down: UPGRADE="
            f"{context['cases_env'].get('UPGRADE')!r}"
        )


def test_a_dockerfile_only_pr_runs_the_cases_on_both_legs(
    clone: Path, tmp_path: Path
) -> None:
    """#5004's Verify-by. The README edit after it is the control: the trigger is the inputs,
    not every PR."""
    _assert_cases_run(clone, tmp_path / "dockerfile", DOCKERFILE)

    for leg, verdicts, _context in _runs_per_leg(
        clone, tmp_path / "control", UNRELATED
    ):
        assert not verdicts[CASES_STEP], (
            f"a PR touching only {UNRELATED} runs the cases on leg {leg}; the trigger has to "
            f"be the cases' inputs, not every pull request"
        )


def test_a_dockerfile_only_pr_builds_the_debs_and_image_the_cases_read(
    clone: Path, tmp_path: Path
) -> None:
    for leg, verdicts, _context in _runs_per_leg(clone, tmp_path, DOCKERFILE):
        skipped = [name for name in CASE_PREREQUISITES if not verdicts.get(name)]
        assert not skipped, (
            f"a PR touching only {DOCKERFILE} skips {skipped} on leg {leg}; the cases read "
            f"the debs and the image those steps produce"
        )


@pytest.mark.parametrize("path", _case_inputs())
def test_a_pr_touching_only_a_case_input_runs_the_cases(
    clone: Path, tmp_path: Path, path: str
) -> None:
    _assert_cases_run(clone, tmp_path, path)
