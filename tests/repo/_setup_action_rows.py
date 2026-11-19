"""The setup action's uv cache entry holds the full environment its key names, as census rows."""

import re

from _row_table import Census, Subject, tracked
from _workflow_rows import SETUP_ACTION, Workflow, judge, mutants, runs, uses

SETUP_UV = "astral-sh/setup-uv"
SYNC = re.compile(r"^uv sync --frozen( --compile-bytecode)?$")


def _action_steps(action: Workflow) -> list[dict]:
    return action["runs"]["steps"]


def _setup_uv_at(action: Workflow) -> int | None:
    return next(
        (i for i, s in enumerate(_action_steps(action)) if uses(s, SETUP_UV)), None
    )


def _setup_uv(action: Workflow) -> dict:
    return next(s for s in _action_steps(action) if uses(s, SETUP_UV))


def _cache_key_problems(action: Workflow) -> list[str]:
    at = _setup_uv_at(action)
    if at is None:
        return [f"no {SETUP_UV} step"]
    suffix = str((_action_steps(action)[at].get("with") or {}).get("cache-suffix", ""))
    return [
        f"the uv cache key does not carry the {var} install profile"
        for var in ("UV_NO_DEV", "UV_NO_GROUP")
        if var not in suffix
    ]


def _full_sync_problems(action: Workflow) -> list[str]:
    at = _setup_uv_at(action)
    if at is None:
        return [f"no {SETUP_UV} step"]
    syncs = [
        s
        for s in _action_steps(action)[at + 1 :]
        if SYNC.match(runs(s).strip()) and s.get("if") == "inputs.uv == 'true'"
    ]
    return (
        []
        if syncs
        else ["no `uv sync --frozen` step after setup-uv, gated on inputs.uv"]
    )


def _drop_the_sync(action: Workflow) -> None:
    _action_steps(action)[:] = [
        s for s in _action_steps(action) if "uv sync" not in runs(s)
    ]


def _sync_before_setup_uv(action: Workflow) -> None:
    action_steps = _action_steps(action)
    sync = next(s for s in action_steps if "uv sync" in runs(s))
    action_steps.remove(sync)
    action_steps.insert(0, sync)


def _sync_a_group(action: Workflow) -> None:
    sync = next(s for s in _action_steps(action) if "uv sync" in runs(s))
    sync["run"] = "uv sync --frozen --only-group lint"


SETUP_ACTION_ROWS = (
    Census(
        name="setup-action-uv-cache-key-carries-the-install-profile",
        reason=(
            "setup-uv keys its cache on `uv.lock` and the Python version, and actions/cache "
            "saves only on a miss, so the first job to finish on a new lockfile writes the "
            "entry every later job restores. promote's `render` job (UV_NO_DEV, three "
            "packages) wrote an 8 MB entry, and a since-deleted CI job with no `uv` call at "
            "all wrote a 1 KB one, so every CI job re-downloaded the dev group and rebuilt "
            "actionlint-py from a GitHub release on every run. A key that carries the install "
            "profile keeps a runtime-only or lint-free job from writing the full key."
        ),
        files=lambda: tracked(SETUP_ACTION),
        offence=judge(_cache_key_problems),
        red=mutants(
            SETUP_ACTION,
            no_profile_in_the_cache_key=lambda a: _setup_uv(a)["with"].pop(
                "cache-suffix"
            ),
            no_lint_group_profile_in_the_cache_key=lambda a: _setup_uv(a)[
                "with"
            ].update(
                {"cache-suffix": "${{ env.UV_NO_DEV == '1' && 'runtime' || 'dev' }}"}
            ),
        ),
        green=(Subject(SETUP_ACTION),),
        must_find=frozenset({SETUP_ACTION}),
    ),
    Census(
        name="setup-action-syncs-the-whole-environment-after-setup-uv",
        reason=(
            "The action syncs the whole install profile itself, so a job that runs no `uv` "
            "command still saves a complete cache entry: a CI job with no `uv` call once "
            "wrote a 1 KB entry that every later job restored. The sync follows setup-uv, is "
            "gated on `inputs.uv`, and syncs no partial group."
        ),
        files=lambda: tracked(SETUP_ACTION),
        offence=judge(_full_sync_problems),
        red=mutants(
            SETUP_ACTION,
            no_sync_step=_drop_the_sync,
            sync_before_uv_is_installed=_sync_before_setup_uv,
            sync_of_a_partial_environment=_sync_a_group,
        ),
        green=(Subject(SETUP_ACTION),),
        must_find=frozenset({SETUP_ACTION}),
    ),
)
