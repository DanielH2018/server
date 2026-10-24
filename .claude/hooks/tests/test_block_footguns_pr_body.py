"""Rule 7 of the block-footguns guard: a PR body that skips a heading of the PR template.

Its own file because test_block_footguns.py was near its line cap. Each case runs in
a throwaway repo under `tmp_path` holding a two-heading template, except the first, which
reads this repo's real template.

Run: uv run pytest .claude/hooks
"""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

_HOOK = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "block-footguns.py"
)
sys.path.insert(0, os.path.dirname(_HOOK))
_spec = importlib.util.spec_from_file_location("block_footguns", _HOOK)
assert _spec and _spec.loader, "spec_from_file_location found no loader"
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

_FULL = "## What changed\n\nCloses #1\n\n## Verification\n\nran it\n"
_PARTIAL = "## What changed\n\nCloses #1\n"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "pull_request_template.md").write_text(
        "<!-- note -->\n## What changed\n\n<!-- why -->\n\n## Verification\n\n- [ ] x\n"
    )
    (tmp_path / "full.md").write_text(_FULL)
    (tmp_path / "partial.md").write_text(_PARTIAL)
    return tmp_path


def _verdict(command: str, cwd: Path) -> str | None:
    return _mod.decide(command, cwd=str(cwd))[1]


def test_this_repos_template_carries_the_headings_the_rule_needs():
    headings = _mod.template_headings(Path(_HOOK).resolve().parents[2])
    assert {"What changed, and why", "Verification"} <= set(headings)


def test_a_create_whose_body_file_skips_a_heading_is_denied(repo):
    reason = _verdict("gh pr create --title t --body-file partial.md", repo)
    assert reason is not None
    assert "`## Verification`" in reason
    assert "`## What changed`" not in reason


def test_a_create_whose_body_file_fills_the_template_is_clean(repo):
    assert _verdict("gh pr create --title t --body-file full.md", repo) is None


def test_an_inline_body_is_judged(repo):
    assert _verdict(f"gh pr create -t t -b '{_PARTIAL}'", repo)
    assert _verdict(f"gh pr create -t t --body='{_FULL}'", repo) is None


def test_a_body_read_from_a_here_document_is_judged(repo):
    command = "gh pr create --title t --body-file - <<'EOF'\n{}EOF"
    assert _verdict(command.format(_PARTIAL), repo)
    assert _verdict(command.format(_FULL), repo) is None


def test_an_edit_that_drops_a_heading_is_denied(repo):
    assert _verdict("gh pr edit 12 --body-file partial.md", repo)


def test_an_edit_that_leaves_the_body_alone_is_clean(repo):
    assert _verdict("gh pr edit 12 --add-label x --title t", repo) is None


def test_fill_with_no_body_is_denied(repo):
    reason = _verdict("gh pr create --fill", repo)
    assert reason is not None
    assert "--fill --body-file" in reason


def test_fill_with_a_fitted_body_file_is_clean(repo):
    assert _verdict("gh pr create --fill --body-file full.md", repo) is None
    assert _verdict("gh pr create --fill --body-file partial.md", repo)


def test_a_create_passing_no_body_is_left_to_gh(repo):
    assert _verdict("gh pr create --title t --draft", repo) is None


def test_a_body_file_relative_to_an_earlier_cd_is_resolved(repo, tmp_path_factory):
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    assert _verdict(f"cd {repo} && gh pr create -t t -F partial.md", elsewhere)
    assert _verdict(f"cd {repo} && gh pr create -t t -F full.md", elsewhere) is None


@pytest.mark.parametrize(
    "command",
    [
        "gh pr create --repo someone/else --title t --body-file partial.md",
        "gh pr edit https://github.com/someone/else/pull/3 --body-file partial.md",
        "gh pr create --title t --body-file missing.md",
    ],
)
def test_another_repo_and_an_unreadable_body_are_clean(repo, command):
    assert _verdict(command, repo) is None


def test_this_repo_named_by_flag_is_still_judged(repo):
    assert _verdict(
        "gh pr create -R DanielH2018/server --title t --body-file partial.md", repo
    )


def test_a_repo_with_no_template_is_clean(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "partial.md").write_text(_PARTIAL)
    assert _verdict("gh pr create --title t --body-file partial.md", tmp_path) is None


def test_the_payload_cwd_reaches_the_rule(repo):
    payload = {
        "tool_input": {"command": "gh pr create --title t --body-file partial.md"},
        "cwd": str(repo),
    }
    assert _mod.decision(payload)[0] == "deny"
