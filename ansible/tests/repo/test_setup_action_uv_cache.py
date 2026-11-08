"""The setup action's uv cache entry holds the full environment its key names.

setup-uv keys its cache on `uv.lock` and the Python version, and actions/cache saves only on a
miss, so the first job to finish on a new lockfile writes the entry every later job restores.
On 2026-11-08 promote's `render` job (UV_NO_DEV, three packages) wrote an 8 MB entry, and
ci.yml's `ansible-lint` job (no `uv` call at all) wrote a 1 KB one. Every CI job then
re-downloaded the dev group and rebuilt actionlint-py from a GitHub release on every run.

Two properties close it. The cache key carries the install profile, so a runtime-only or
lint-free job never writes the full key, and the action syncs the whole profile itself, so a
job that runs no `uv` command still saves a complete entry.

Run: uv run pytest ansible/tests/repo/test_setup_action_uv_cache.py
"""

import copy
import re

import pytest
from _helpers import REPO
from lib import yaml_fast

ACTION = REPO / ".github" / "actions" / "setup" / "action.yml"
SETUP_UV = "astral-sh/setup-uv"
SYNC = re.compile(r"^uv sync --frozen$")


def _steps(action: dict) -> list[dict]:
    return action["runs"]["steps"]


def _setup_uv_index(steps: list[dict]) -> int | None:
    return next(
        (
            i
            for i, s in enumerate(steps)
            if str(s.get("uses", "")).startswith(SETUP_UV + "@")
        ),
        None,
    )


def problems(action: dict) -> list[str]:
    steps = _steps(action)
    at = _setup_uv_index(steps)
    if at is None:
        return ["no astral-sh/setup-uv step"]
    found = []
    args = steps[at].get("with") or {}
    suffix = str(args.get("cache-suffix", ""))
    for var in ("UV_NO_DEV", "UV_NO_GROUP"):
        if var not in suffix:
            found.append(f"the uv cache key does not carry the {var} install profile")
    syncs = [
        s
        for s in steps[at + 1 :]
        if SYNC.match(str(s.get("run", "")).strip())
        and s.get("if") == "inputs.uv == 'true'"
    ]
    if not syncs:
        found.append("no `uv sync --frozen` step after setup-uv, gated on inputs.uv")
    return found


@pytest.fixture(scope="module")
def action() -> dict:
    return yaml_fast.safe_load(ACTION.read_text(encoding="utf-8"))


def test_the_setup_action_saves_a_complete_uv_cache(action):
    assert problems(action) == []


def _drop_suffix(action):
    step = next(
        s for s in _steps(action) if str(s.get("uses", "")).startswith(SETUP_UV + "@")
    )
    del step["with"]["cache-suffix"]


def _suffix_without_the_lint_profile(action):
    step = next(
        s for s in _steps(action) if str(s.get("uses", "")).startswith(SETUP_UV + "@")
    )
    step["with"]["cache-suffix"] = "${{ env.UV_NO_DEV == '1' && 'runtime' || 'dev' }}"


def _drop_sync(action):
    _steps(action)[:] = [
        s for s in _steps(action) if "uv sync" not in str(s.get("run", ""))
    ]


def _sync_before_setup_uv(action):
    steps = _steps(action)
    sync = next(s for s in steps if "uv sync" in str(s.get("run", "")))
    steps.remove(sync)
    steps.insert(0, sync)


def _sync_a_group(action):
    sync = next(s for s in _steps(action) if "uv sync" in str(s.get("run", "")))
    sync["run"] = "uv sync --frozen --only-group lint"


RED = {
    "no profile in the cache key": (_drop_suffix, "does not carry the UV_NO_DEV"),
    "no lint-group profile in the cache key": (
        _suffix_without_the_lint_profile,
        "does not carry the UV_NO_GROUP",
    ),
    "no sync step": (_drop_sync, "no `uv sync --frozen` step"),
    "sync before uv is installed": (
        _sync_before_setup_uv,
        "no `uv sync --frozen` step",
    ),
    "sync of a partial environment": (_sync_a_group, "no `uv sync --frozen` step"),
}


@pytest.mark.parametrize("case", RED, ids=list(RED))
def test_each_broken_property_is_named(action, case):
    mutate, expected = RED[case]
    broken = copy.deepcopy(action)
    mutate(broken)
    assert broken != action, f"{case}: the mutation left the action unchanged"
    assert any(expected in line for line in problems(broken)), problems(broken)
