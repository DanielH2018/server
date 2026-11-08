"""repo_guards --drop-candidates proposes a guard only when the CI failure survey counted no failure in it (#7445).

The tests run the entry point as a subprocess against the fake GitHub `test_ci_failures.py`
uses, so they hold whether `repo_guards` imports `ci_failures` or runs it.

Run: uv run pytest scripts/dev/tests/test_repo_guards_survey.py
"""

import json
import re
import sys
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import repo_guards
from lib.proc_testing import fake_bin, run
from lib.repo_paths import REPO

SCRIPT = REPO / "scripts" / "dev" / "repo_guards.py"
FAKE_GITHUB = Path(__file__).with_name("ci_failures_fake_github.txt")

# The epoch a dated run is measured from. `main` takes no `now=`, so the shim pins the child's
# `datetime.now` and `today` to it before running the entry point as `__main__`.
NOW = datetime(2026, 11, 8, 12, tzinfo=UTC)
_FROZEN_CLOCK = (
    "import datetime, runpy, sys\n"
    "epoch = datetime.datetime.fromisoformat(sys.argv[1])\n"
    "class FrozenDate(datetime.date):\n"
    "    @classmethod\n"
    "    def today(cls):\n"
    "        return cls.fromordinal(epoch.date().toordinal())\n"
    "class Frozen(datetime.datetime):\n"
    "    @classmethod\n"
    "    def now(cls, tz=None):\n"
    "        return cls.fromtimestamp(epoch.timestamp(), tz)\n"
    "    @classmethod\n"
    "    def today(cls):\n"
    "        return cls.now()\n"
    "datetime.date = FrozenDate\n"
    "datetime.datetime = Frozen\n"
    "sys.argv = sys.argv[2:]\n"
    "sys.path[0] = sys.argv[0].rpartition('/')[0]\n"
    "runpy.run_path(sys.argv[0], run_name='__main__')\n"
)


def _red_run(run_id: int, created: datetime, *modules: str) -> dict:
    """A CI run red on its only attempt, its pytest step's summary naming each module."""
    stamp = created.strftime("%Y-%m-%dT%H:%M:%S")
    lines = [
        "##[group]Run uv run pytest",
        "##[endgroup]",
        "=== short test summary info ===",
        *(f"FAILED {m}::test_case - assert 1 == 2" for m in modules),
        "##[error]Process completed with exit code 1.",
    ]

    def step(name: str, number: int, conclusion: str, text: list[str]) -> dict:
        stamped = [f"{stamp}.0000000Z {line}" for line in text]
        stamped[0] = "\ufeff" + stamped[0]
        return {
            "name": name,
            "number": number,
            "conclusion": conclusion,
            "lines": stamped,
        }

    job = {
        "id": run_id + 90_000_000_000,
        "name": "pytest",
        "conclusion": "failure",
        "steps": [
            step("Set up job", 1, "success", ["Job started"]),
            step("Run the suite", 5, "failure", lines),
        ],
    }
    return {
        "id": run_id,
        "name": "CI",
        "event": "pull_request",
        "head_branch": "fix-guard",
        "created_at": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "attempts": [{"conclusion": "failure", "jobs": [job]}],
    }


def _module(guard: str) -> str:
    """A test module the guard covers: its own file, or one beneath its directory."""
    path = guard.split("::", 1)[0]
    return path if path.endswith(".py") else f"{path}/test_counted.py"


def _droppable() -> list[str]:
    """The guards no hook pins, the only ones `--drop-candidates` can ever name."""
    hooks = repo_guards.hook_files()
    return [g for g in repo_guards.GUARDS if not any(h.startswith(g) for h in hooks)]


def _drop_candidates(tmp_path: Path, *runs: dict) -> set[str]:
    """Run `--drop-candidates --weeks 1` against a fake GitHub holding `runs`; return the guards named."""
    fake = f"#!{sys.executable} -IS\n{FAKE_GITHUB.read_text()}"
    stubs = fake_bin(tmp_path / "bin", gh=fake, ghread=fake)
    (stubs / "world.json").write_text(json.dumps(runs))
    argv = [sys.executable, "-c", _FROZEN_CLOCK, NOW.isoformat(), str(SCRIPT)]
    argv += ["--drop-candidates", "--weeks", "1"]
    done = run(argv, cwd=REPO, stub_bin=stubs)
    assert done.returncode == 0, f"exited {done.returncode}: {done.stderr}"
    return {
        guard
        for guard in repo_guards.GUARDS
        if re.search(rf"(?<![\w/.]){re.escape(guard)}(?![\w/.]|::)", done.stdout)
    }


def test_drop_candidates_names_every_guard_but_those_the_survey_counted_failing(
    tmp_path,
):
    """The issue's Verify-by: a failure the survey counted in the window keeps its guard (#7445)."""
    droppable = _droppable()
    failing = set(droppable[:2])
    assert len(failing) == 2 and set(droppable) - failing, droppable
    red = _red_run(
        46000000001, NOW - timedelta(days=1), *(_module(g) for g in sorted(failing))
    )
    assert _drop_candidates(tmp_path, red) == set(droppable) - failing


def test_a_failure_before_the_window_does_not_keep_its_guard(tmp_path):
    droppable = _droppable()
    red = _red_run(46000000002, NOW - timedelta(weeks=3), _module(droppable[0]))
    assert droppable[0] in _drop_candidates(tmp_path, red)


def test_the_guard_list_keeps_no_hand_kept_date():
    """The survey's tally replaces each guard's date of its last counted failure (#7445)."""
    guards = repo_guards.GUARDS
    values = guards.values() if isinstance(guards, Mapping) else []
    dated = []
    for guard, value in zip(guards, values, strict=False):
        try:
            date.fromisoformat(str(value))
        except ValueError:
            continue
        dated.append(guard)
    assert dated == []
