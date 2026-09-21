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

pytestmark = pytest.mark.usefixtures("segmenter_or_skip")


def test_deploy_sh_with_the_staleness_flag_is_denied():
    assert "staging_gate_remote.sh" in _mod.problem(
        "./scripts/deploy.sh --tags sonarr --skip-staleness-check"
    )


def test_the_flag_before_the_tags_is_denied():
    assert _mod.problem("scripts/deploy.sh --skip-staleness-check --tags sonarr")


def test_a_keyword_prefixed_deploy_is_denied():
    assert _mod.problem("! ./scripts/deploy.sh --tags sonarr --skip-staleness-check")


def test_deploy_sh_without_the_flag_is_clean():
    assert _mod.problem("./scripts/deploy.sh --tags sonarr") is None


def test_the_staging_gate_invocation_is_clean():
    """The one sanctioned use is inside staging_gate_remote.sh's own text. DECIDED: this
    case cannot fire — the flag never appears in the invocation — and the test stays, because
    it is the boundary the issue (#2170) named and a widened rule keyed on the flag alone
    would cross it."""
    assert (
        _mod.problem(
            "./scripts/deploy_tools/staging_gate_remote.sh abc1234 sonarr,radarr"
        )
        is None
    )


def test_the_flag_as_a_grep_argument_is_clean():
    """Keyed on the command word, not the flag: reading about the flag is not using it."""
    assert _mod.problem("grep -rn -- --skip-staleness-check scripts/ docs/") is None


def test_the_flag_gates_could_fire_where_the_binary_regex_cannot():
    """`./scripts/deploy.sh` sits behind a `/`, which `_RULE_BINARY_RE`'s lookbehind refuses;
    the flag literal is what opens the ask gate on an unsplittable command."""
    assert _mod.could_fire("./scripts/deploy.sh --tags x --skip-staleness-check")
    assert not _mod.could_fire("./scripts/deploy.sh --tags x")
