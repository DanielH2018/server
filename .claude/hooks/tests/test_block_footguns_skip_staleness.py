"""Rule 9 of the block-footguns guard: `deploy.sh --skip-staleness-check` typed as a command.

Its own file because test_block_footguns.py sits at the 500-line test cap; the loader is
the same, so `_mod` here is the same hook module.

Run: uv run pytest .claude/hooks
"""

import importlib.util
import os
import sys

import pytest

_HOOK = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "block-footguns.py"
)
sys.path.insert(0, os.path.dirname(_HOOK))
_spec = importlib.util.spec_from_file_location("block_footguns", _HOOK)
assert _spec and _spec.loader, "spec_from_file_location found no loader"
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

# The rule itself, handed a stage directly, apart from the segmenter the `problem()` tests
# below also run through.


def test_the_rule_denies_a_deploy_sh_stage_carrying_the_flag():
    assert _mod.skip_staleness_problem(
        ["./scripts/deploy.sh", "--tags", "sonarr", "--skip-staleness-check"]
    )


def test_the_rule_ignores_a_deploy_sh_stage_without_the_flag():
    assert (
        _mod.skip_staleness_problem(["./scripts/deploy.sh", "--tags", "sonarr"]) is None
    )


def test_the_rule_ignores_the_flag_behind_another_binary():
    assert (
        _mod.skip_staleness_problem(["grep", "-rn", "--skip-staleness-check"]) is None
    )


@pytest.mark.parametrize(
    "module",
    [
        "scripts/deploy_tools/deploy_cli.py",
        # What the shim execed before #4347, still present in an older checkout.
        "scripts/deploy_tools/deploy_run.py",
    ],
)
def test_the_rule_denies_the_python_module_the_shim_execs(module):
    """`deploy.sh` execs `deploy_cli.py`; running it directly is the same deploy."""
    assert _mod.skip_staleness_problem(
        ["uv", "run", "python", module, "--tags", "sonarr", "--skip-staleness-check"]
    )


def test_the_rule_ignores_a_grep_naming_the_module_and_the_flag():
    assert (
        _mod.skip_staleness_problem(
            [
                "grep",
                "--",
                "--skip-staleness-check",
                "scripts/deploy_tools/deploy_cli.py",
            ]
        )
        is None
    )


def test_the_rule_is_registered():
    assert _mod.skip_staleness_problem in _mod._RULES


def test_deploy_sh_with_the_staleness_flag_is_denied():
    assert "land.sh --at" in _mod.problem(
        "./scripts/deploy.sh --tags sonarr --skip-staleness-check"
    )


def test_the_flag_before_the_tags_is_denied():
    assert _mod.problem("scripts/deploy.sh --skip-staleness-check --tags sonarr")


def test_a_keyword_prefixed_deploy_is_denied():
    assert _mod.problem("! ./scripts/deploy.sh --tags sonarr --skip-staleness-check")


def test_deploy_sh_without_the_flag_is_clean():
    assert _mod.problem("./scripts/deploy.sh --tags sonarr") is None


def test_a_wrapper_script_that_passes_the_flag_itself_is_clean():
    """The rule is keyed on the command word, so a script that passes the flag from inside its
    own text never reaches it. A stand-in script proves the boundary: a widened rule keyed on
    the flag alone would cross it."""
    assert _mod.problem("./scripts/some_wrapper.sh abc1234 sonarr,radarr") is None


def test_the_flag_as_a_grep_argument_is_clean():
    """Keyed on the command word, not the flag: reading about the flag is not using it."""
    assert _mod.problem("grep -rn -- --skip-staleness-check scripts/ docs/") is None


def test_the_flag_gates_could_fire_where_the_binary_regex_cannot():
    """`./scripts/deploy.sh` sits behind a `/`, which `_RULE_BINARY_RE`'s lookbehind refuses;
    the flag literal is what opens the ask gate on an unsplittable command."""
    assert _mod.could_fire("./scripts/deploy.sh --tags x --skip-staleness-check")
    assert not _mod.could_fire("./scripts/deploy.sh --tags x")
