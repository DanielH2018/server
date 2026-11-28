"""Each binary a test module skips without is one the pytest-tools action requires, or absent.

A runner that lacks such a binary passes the tests gated on it as skips, so its run reads green
while those tests checked nothing. The action's last step fails a leg that lacks one, so a
runner pod's run can be set beside a hosted run's (docs/ci-throughput.md, Phase C). This census
keeps that step's `BASELINE` in step with the suite's skip gates.

Run: uv run pytest tests/repo/ci_workflows/test_ci_baseline_binaries.py
"""

import re
import shutil
import subprocess
from pathlib import Path

from _helpers import REPO
from lib import yaml_fast
from lib.proc_testing import fake_bin, run

ACTION = REPO / ".github" / "actions" / "pytest-tools" / "action.yml"
STEP = "Fail on a missing binary the suite skips without"
# Skip-gated binaries that no CI leg is meant to have, each with why.
ABSENT = {
    "strace": "the hosted image ships none; the traced record runs on the hosts",
    "actionlint": "it is in the `lint` group, which the pytest job leaves out",
    "pyright": "it is in the `lint` group, which the pytest job leaves out",
}
# Gates the census must find, so a reworded skip cannot leave it reading nothing.
KNOWN_GATES = frozenset(
    {"visudo", "sshd", "node", "systemd-analyze", "rsync", "strace"}
)
_MARK = re.compile(r'skipif\(\s*(?:not\s+)?shutil\.which\(\s*"([\w.+-]+)"')
_IMPERATIVE = re.compile(
    r'which\(\s*"([\w.+-]+)"[^\n]*\n(?:[^\n]*\n){0,2}?[^\n]*pytest\.skip\('
)


def gated_binaries(texts: dict[str, str]) -> dict[str, set[str]]:
    """Each binary a module skips without, mapped to the modules that gate on it.

    A gate is a `skipif(shutil.which("x") ...)` mark, or a `which("x")` with a `pytest.skip(`
    no more than three lines below it.
    """
    gates: dict[str, set[str]] = {}
    for path, text in texts.items():
        for pattern in (_MARK, _IMPERATIVE):
            for binary in pattern.findall(text):
                gates.setdefault(binary, set()).add(path)
    return gates


def unlisted(
    gates: dict[str, set[str]], baseline: frozenset[str]
) -> dict[str, set[str]]:
    """The gated binaries neither in `baseline` nor in `ABSENT`."""
    return {
        b: mods for b, mods in gates.items() if b not in baseline and b not in ABSENT
    }


def _test_module_texts() -> dict[str, str]:
    listed = run(["git", "ls-files", "-z", "--", "*.py"], cwd=REPO, check=True)
    listed = listed.stdout.split("\0")
    itself = Path(__file__).resolve().relative_to(REPO).as_posix()
    return {
        path: (REPO / path).read_text(encoding="utf-8")
        for path in listed
        if (path.startswith("tests/") or "/tests/" in path) and path != itself
    }


def _step() -> dict:
    steps = yaml_fast.safe_load(ACTION.read_text(encoding="utf-8"))["runs"]["steps"]
    return next(s for s in steps if s.get("name") == STEP)


def _baseline() -> frozenset[str]:
    return frozenset(_step()["env"]["BASELINE"].split())


def _run_step(tmp_path, present: set[str]) -> subprocess.CompletedProcess[str]:
    """The step's script, on a PATH holding a stub for each name in `present` and nothing else."""
    bash = shutil.which("bash")
    assert bash is not None
    bin_dir = fake_bin(tmp_path / "bin", **dict.fromkeys(present, ""))
    step = _step()
    return run(
        [bash, "-c", step["run"]],
        env={"PATH": str(bin_dir), "BASELINE": step["env"]["BASELINE"]},
    )


def test_every_skip_gated_binary_is_required_or_named_absent():
    gates = gated_binaries(_test_module_texts())
    assert KNOWN_GATES <= set(gates), f"census lost {KNOWN_GATES - set(gates)}"
    assert unlisted(gates, _baseline()) == {}, (
        f"add each to BASELINE in {STEP!r}, or to ABSENT with why no leg has it"
    )


def test_a_gate_on_an_unlisted_binary_is_flagged():
    text = '@pytest.mark.skipif(shutil.which("jq") is None, reason="needs jq")\n'
    assert unlisted(gated_binaries({"t/test_x.py": text}), frozenset()) == {
        "jq": {"t/test_x.py"}
    }


def test_an_imperative_skip_gate_is_found():
    text = 'real = shutil.which("rsync", path=p)\nif real is None:\n    pytest.skip("no rsync")\n'
    assert gated_binaries({"t/test_x.py": text}) == {"rsync": {"t/test_x.py"}}


def test_the_step_with_every_baseline_binary_on_path_is_clean(tmp_path):
    done = _run_step(tmp_path, set(_baseline()))
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout == f"{len(_baseline())} baseline binaries on PATH\n"


def test_the_step_with_a_baseline_binary_missing_is_flagged(tmp_path):
    done = _run_step(tmp_path, set(_baseline()) - {"sshd"})
    assert done.returncode == 1
    assert "::error::not on PATH: sshd." in done.stdout
