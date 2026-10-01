"""Where `_deploy_fakes` answers a boundary, it must answer it the way production does.

A fake that is stricter than production is not a safe fake. It reads green forever — every
test that would have caught the divergence fails to be written, because the fake refuses the
input production accepts. That is the "green while checking nothing" class from the repo's
testing rules, arriving through the test double rather than through the check.

The image-diff seam's tests below are a pair: an input the aligned fake must ACCEPT
and one it must REJECT, so a fake that went permissive everywhere fails as loudly as one that
went strict everywhere.

Run: uv run pytest ansible/roles/setup/gitops_deploy/tests/test_deploy_fakes_match_production.py
"""

import pathlib

import pytest

from _deploy_fakes import LOCAL, ORIGIN, ScriptedTick

# The `git diff -U0` argv `deploy_io.k8s_image_diff` builds, which is what the fake parses the
# service name out of. Built here from the same shape rather than hand-written, so a change to
# the path layout breaks this file instead of silently missing the branch.
DIFF_ARGV = [
    "git",
    "diff",
    "-U0",
    f"{LOCAL}..{ORIGIN}",
    "--",
    "ansible/roles/k8s/sonarr/defaults/main.yml",
]


@pytest.fixture
def tick(tmp_path: pathlib.Path) -> ScriptedTick:
    return ScriptedTick(tmp_path)


def test_an_unscripted_image_diff_is_rejected(tick: ScriptedTick):
    """The reject half for the one `run()` path that could default instead of raising.

    `self.diffs.get(key, "")` would hand back an empty diff for a service nobody scripted, and
    an empty diff is a real production answer meaning "the range touched no line of that file".
    A test that forgot to script one would therefore read as a clean no-op and pass.
    """
    with pytest.raises(AssertionError, match="no test scripted"):
        tick.run(DIFF_ARGV)


def test_a_scripted_empty_image_diff_is_still_answered(tick: ScriptedTick):
    """The accept half: an empty diff is legitimate, so long as the test says so on purpose."""
    tick.diffs["sonarr"] = ""
    assert tick.run(DIFF_ARGV) == ""
