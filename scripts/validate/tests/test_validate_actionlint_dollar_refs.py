"""Each test lints a scratch tree holding the real setup action and one caller workflow.

Two tests run the real actionlint binary, which only the `lint` dependency group installs. CI's
pytest shards skip that group, so the `actionlint-red-proof` prek hook runs this module in the
hooks job instead.

Run: uv run pytest scripts/validate/tests/test_validate_actionlint_dollar_refs.py
"""

import shutil

import pytest
from lib.repo_paths import REPO
from validate import actionlint_dollar_refs

needs_actionlint = pytest.mark.skipif(
    shutil.which("actionlint") is None,
    reason="actionlint is in the lint group; the actionlint-red-proof prek hook runs this",
)

CALLER = """\
on: push
permissions: {}
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: REF/.github/actions/setup
        with:
          uv: "false"
EXTRA"""


def _tree(tmp_path, ref: str = "$", extra: str = "") -> str:
    action = tmp_path / ".github/actions/setup/action.yml"
    action.parent.mkdir(parents=True)
    shutil.copy2(REPO / ".github/actions/setup/action.yml", action)
    workflow = tmp_path / ".github/workflows/caller.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(CALLER.replace("REF", ref).replace("EXTRA", extra))
    return ".github/workflows/caller.yml"


@needs_actionlint
def test_a_dollar_caller_with_known_inputs_is_clean(tmp_path, capfd):
    path = _tree(tmp_path)
    assert actionlint_dollar_refs.lint([path], tmp_path) == 0, capfd.readouterr()


@needs_actionlint
def test_a_dollar_caller_with_an_undefined_input_is_flagged(tmp_path, capfd):
    path = _tree(tmp_path, extra="          no-such-input: x\n")
    assert actionlint_dollar_refs.lint([path], tmp_path) != 0
    out = capfd.readouterr().out
    assert f'{path}:11:11: input "no-such-input" is not defined' in out, out


def test_a_tree_with_no_dollar_ref_left_is_flagged(tmp_path, capfd):
    path = _tree(tmp_path, ref=".")
    assert actionlint_dollar_refs.lint([path], tmp_path) == 1
    assert "[FAIL] .github/workflows: no `uses: $/` ref" in capfd.readouterr().err
