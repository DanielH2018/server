"""A PR that edits a hook's configuration must run that hook, on the PR and not only on master.

On a PR, ci.yml runs the prek hooks scoped to the files the PR changed, and each hook's `files`
regex in prek.toml matches the code the hook checks. A hook's CONFIG file is not the code it
checks, so a PR that only edited the config ran that hook against nothing, and master's
`--all-files` sweep failed after the merge. Over the 1000 master runs before 2026-09-11 that was
every master-only lint failure there was: Vale's terms on 2026-08-27 (`.vale.ini`, runs
33126290755 and 33126512503) and ruff's rules on 2026-09-02 (pyproject.toml, runs 33632377189
and 33632621818), each shown twice because a second commit landed before the fix.

Two mechanisms close it, and which one a hook uses follows from whether it takes filenames:

- A hook with `pass_filenames = false` names its config in `files`. Any match runs it over the
  whole tree, so the config file is just one more trigger. `ty` did this first.
- A hook that takes filenames cannot: prek would hand it the config file to lint. For those the
  scoping step in ci.yml carries a `FULL_SWEEP_PATHS` list, and a PR touching one of them runs
  the full sweep. `prek.toml` is in every job's list because a rev bump changes what any hook
  does and matches no hook's `files` but check-toml's.

A hook's config is more than one file, and the first version of this test treated it as one --
so `uv.lock`, which decides the ruff VERSION both ruff hooks run, and Vale's vocabulary and
binary pin were uncovered while every hook read as clean (#2543, #2552). `CONFIG_OWNERS` below
maps a hook to every file that changes its verdict, and each is checked on its own.

The static half here checks each hook against that rule, reading prek.toml and both lists. The
behavioural half runs the scoping steps' own scripts against a scratch repository, once per
listed path, so a list the script stopped reading -- or one entry of it the `grep -Fx` match
cannot see -- fails here rather than passing on its presence in the YAML.

Run: uv run pytest ansible/tests/repo/test_hook_config_changes_run_the_hook.py
"""

import re
import tomllib
from collections.abc import Mapping
from pathlib import Path

import pytest
from _ci_scoping import (
    CI_WORKFLOW,
    checkout_merge_ref,
    commit,
    full_sweep_paths,
    git,
    make_clone,
    run_step,
)
from _helpers import REPO

PREK_CONFIG = REPO / "prek.toml"

# Hook id -> (every file that changes this hook's verdict, the ci.yml job that runs it). Every
# entry must exist in prek.toml and every config file in the tree, so a hook rename or a config
# move fails here by name rather than silently shrinking what the rule below covers.
#
# A hook's config is not always one file, which is how two of these stayed uncovered after the
# single-file version of this test landed:
#
# - uv.lock decides the VERSION of a tool a hook runs under `uv run --frozen`, so it changes the
#   verdict the way the ruleset does. Renovate's lock-file maintenance moved ruff 0.16.8 ->
#   0.16.9 with both ruff hooks reading "(no files to check)Skipped" (#2552).
# - Vale reads three: its ini, its vocabulary (which decides what Vale.Spelling accepts across
#   the whole corpus), and the workflow that pins the binary. The 3.21.0 -> 3.22.0 bump turned
#   master red on 15 untouched lines in 10 docs with Vale skipped on its own PR (#2536, #2543).
#
# The host's Vale pin (ansible/roles/setup/initial_setup/tasks/host-basics.yml) is NOT a config
# file here: CI installs its own binary, and test_vale_matches_the_ci_pin.py already forces a
# bump to move both pins in one commit.
CONFIG_OWNERS: dict[str, tuple[tuple[str, ...], str]] = {
    "ruff": (("pyproject.toml", "uv.lock"), "hooks"),
    "ruff-format": (("pyproject.toml", "uv.lock"), "hooks"),
    "ty": (("pyproject.toml", "uv.lock"), "hooks"),
    "mkdocs-strict": (("mkdocs.yml", "uv.lock"), "hooks"),
    "vale": (
        (
            ".vale.ini",
            "styles/config/vocabularies/Homelab/accept.txt",
            ".github/workflows/ci.yml",
        ),
        "hooks",
    ),
    "shellcheck": ((".shellcheckrc",), "hooks"),
    "gitleaks": ((".gitleaks.toml",), "hooks"),
    "ansible-lint": ((".ansible-lint",), "ansible_lint"),
}

# The jobs whose scoping step carries a FULL_SWEEP_PATHS list, and what every list must hold.
SCOPED_JOBS = frozenset({"hooks", "ansible_lint"})
ALWAYS_TRIGGERS = frozenset({"prek.toml"})


def _hooks_by_id(prek_text: str) -> dict[str, dict]:
    data = tomllib.loads(prek_text)
    return {
        hook["id"]: hook
        for repo in data.get("repos", [])
        for hook in repo.get("hooks", [])
    }


def uncovered(
    hooks: dict[str, dict],
    triggers: dict[str, list[str]],
    owners: Mapping[str, tuple[tuple[str, ...], str]],
) -> set[tuple[str, str]]:
    """(hook id, config) pairs whose edit would run neither the hook nor the full sweep.

    A hook that takes no filenames is covered when its `files` regex matches the config; one
    that does is covered when the config sits in its job's FULL_SWEEP_PATHS. Every config a
    hook owns is checked on its own, so one covered file cannot vouch for a sibling that is
    not. `pass_filenames` defaults to true, as it does in prek, so a remote hook whose
    definition prek.toml does not carry is held to the CI list -- the direction that cannot
    under-cover."""
    missing = set()
    for hook_id, (configs, job) in owners.items():
        hook = hooks[hook_id]
        for config in configs:
            if hook.get("pass_filenames", True):
                if config not in triggers.get(job, []):
                    missing.add((hook_id, config))
            elif not re.search(hook.get("files", "^$"), config):
                missing.add((hook_id, config))
    return missing


def _live_triggers() -> dict[str, list[str]]:
    text = CI_WORKFLOW.read_text()
    return {job: full_sweep_paths(text, job) for job in SCOPED_JOBS}


def test_every_owner_names_a_real_hook_and_a_real_config() -> None:
    hooks = _hooks_by_id(PREK_CONFIG.read_text())
    assert set(CONFIG_OWNERS) <= set(hooks), sorted(set(CONFIG_OWNERS) - set(hooks))
    for hook_id, (configs, _job) in CONFIG_OWNERS.items():
        for config in configs:
            assert (REPO / config).is_file(), (
                f"{hook_id}: {config} is not a file in the tree"
            )


def test_every_hook_config_edit_runs_that_hook_on_the_pr() -> None:
    missing = uncovered(
        _hooks_by_id(PREK_CONFIG.read_text()), _live_triggers(), CONFIG_OWNERS
    )
    assert not missing, (
        f"a PR editing only these configs would run their hook on master alone: "
        f"{sorted(missing)}. A hook with pass_filenames=false names its config in `files`; "
        f"one that takes filenames goes in its job's FULL_SWEEP_PATHS in ci.yml."
    )


def test_every_scoping_step_forces_the_sweep_on_a_prek_toml_edit() -> None:
    triggers = _live_triggers()
    for job in SCOPED_JOBS:
        assert ALWAYS_TRIGGERS <= set(triggers[job]), (job, triggers[job])


def test_a_no_filenames_hook_whose_files_omits_its_config_is_flagged() -> None:
    hooks = {"ruff": {"id": "ruff", "pass_filenames": False, "files": r"\.py$"}}
    owners = {"ruff": (("pyproject.toml",), "hooks")}
    assert uncovered(hooks, {"hooks": ["prek.toml"]}, owners) == {
        ("ruff", "pyproject.toml")
    }


def test_a_no_filenames_hook_whose_files_names_its_config_is_clean() -> None:
    hooks = {
        "ruff": {
            "id": "ruff",
            "pass_filenames": False,
            "files": r"(\.py$|^pyproject\.toml$)",
        }
    }
    owners = {"ruff": (("pyproject.toml",), "hooks")}
    assert uncovered(hooks, {"hooks": ["prek.toml"]}, owners) == set()


def test_one_covered_config_does_not_vouch_for_the_hooks_other_config() -> None:
    # The multi-config half: ruff's ruleset is named in `files`, its pin is not, which is
    # exactly the state #2552 reported. A per-hook verdict would have read this as clean.
    hooks = {
        "ruff": {
            "id": "ruff",
            "pass_filenames": False,
            "files": r"(\.py$|^pyproject\.toml$)",
        }
    }
    owners = {"ruff": (("pyproject.toml", "uv.lock"), "hooks")}
    assert uncovered(hooks, {"hooks": ["prek.toml"]}, owners) == {("ruff", "uv.lock")}


def test_a_filenames_hook_missing_from_its_jobs_list_is_flagged() -> None:
    hooks = {"vale": {"id": "vale", "files": r"^docs/.*\.md$"}}
    owners = {"vale": ((".vale.ini",), "hooks")}
    # Present in the OTHER job's list does not count: each job computes its own.
    assert uncovered(hooks, {"ansible_lint": [".vale.ini"]}, owners) == {
        ("vale", ".vale.ini")
    }


def test_a_filenames_hook_in_its_jobs_list_is_clean() -> None:
    hooks = {"vale": {"id": "vale", "files": r"^docs/.*\.md$"}}
    owners = {"vale": ((".vale.ini",), "hooks")}
    assert uncovered(hooks, {"hooks": [".vale.ini"]}, owners) == set()


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    return make_clone(tmp_path)


def _pr_editing(clone: Path, path: str) -> None:
    git(clone, "checkout", "-q", "-b", "pr")
    commit(clone, f"edit {path}", **{path: "changed\n"})
    checkout_merge_ref(clone, "pr")


def _every_listed_trigger() -> list[tuple[str, str]]:
    """(job, path) for every live FULL_SWEEP_PATHS entry, so each one is exercised.

    Parametrizing over the first entry alone only ever ran `prek.toml`: an entry added to the
    YAML got the static check above and no behavioural one, and the scoping step matches with
    `grep -Fx`, which a path the block scalar indents wrong or spells with a glob silently
    fails. The scratch repository is a two-file bare clone, so a case costs milliseconds."""
    triggers = _live_triggers()
    return [(job, path) for job in sorted(SCOPED_JOBS) for path in triggers[job]]


@pytest.mark.parametrize(("job", "trigger"), _every_listed_trigger())
def test_a_pr_editing_a_listed_config_runs_the_full_sweep(
    clone: Path, tmp_path: Path, job: str, trigger: str
) -> None:
    _pr_editing(clone, trigger)

    proc, output = run_step(clone, tmp_path, job)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "mode=full" in output, (trigger, proc.stdout)


def test_the_hooks_list_does_not_leak_into_the_ansible_lint_job(
    clone: Path, tmp_path: Path
) -> None:
    # `.vale.ini` is the `hooks` job's concern. To the ansible-lint job it is a non-ansible
    # file, so its step reports nothing to lint rather than a full tree walk.
    assert ".vale.ini" not in _live_triggers()["ansible_lint"]
    _pr_editing(clone, ".vale.ini")

    proc, output = run_step(clone, tmp_path, "ansible_lint")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "mode=none" in output, proc.stdout


def test_a_pr_editing_a_task_file_alone_stays_scoped(
    clone: Path, tmp_path: Path
) -> None:
    _pr_editing(clone, "ansible/roles/x/tasks/main.yml")

    proc, output = run_step(clone, tmp_path, "ansible_lint")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "mode=scoped" in output, proc.stdout
