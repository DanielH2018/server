"""`deploy_by_host`'s tag-lookup failure must not collide with deploy.sh's own exit 1.

`deploy_tags.py hosts` failing (a crash, a bad environment) exits 1 -- Python's default for
an uncaught exception, since `_cmd_hosts` never returns non-zero on its own. deploy.sh has
its own rare `cd "$repo_root" || exit 1`. Both used to feed the same `$deploy_rc`, landing in
land.sh's catch-all `deploy-failed (exit $deploy_rc)` arm -- indistinguishable from a real
deploy.sh failure even though deploy.sh never ran at all (issue #1016). The fix reserves
HOST_LOOKUP_FAILED=21, the same shape as PLAYBOOK_FAILED=20 in deploy.sh
(test_landing_annotations.py and this file's siblings cover that one; this covers the twin).

Textual, like test_land_stale_retry_waits_on_tip.py: land.sh has no cheap live test.
"""

from __future__ import annotations

import re

from _helpers import REPO as _REPO

_LAND_SH = _REPO / "scripts/deploy_tools/land.sh"
_DEPLOY_SH = _REPO / "scripts/deploy.sh"


def _text() -> str:
    return _LAND_SH.read_text()


def _host_lookup_line(text: str) -> str:
    for line in text.splitlines():
        if "deploy_tags.py hosts" in line and "by_host=" in line:
            return line
    raise AssertionError("deploy_by_host no longer calls `deploy_tags.py hosts`")


def test_the_host_lookup_failure_does_not_return_bare_1():
    """The bug: `|| return 1` collides with deploy.sh's own `exit 1`."""
    line = _host_lookup_line(_text())
    assert not re.search(r"\|\|\s*return\s+1\b", line), (
        f"deploy_by_host returns bare 1 on a lookup failure, colliding with deploy.sh's "
        f"own exit 1: {line!r}"
    )


def test_a_bare_return_1_on_lookup_failure_would_be_caught():
    """The reject half: prove the check above can fail, on the pre-fix shape."""
    mutated = (
        'by_host=$(uv run python scripts/deploy_tools/deploy_tags.py hosts "$TAGS") '
        "|| return 1"
    )
    line = _host_lookup_line(mutated)
    assert re.search(r"\|\|\s*return\s+1\b", line)


def test_host_lookup_failed_is_a_literal_assignment():
    m = re.search(r"^HOST_LOOKUP_FAILED=(\d+)$", _text(), re.MULTILINE)
    assert m, "HOST_LOOKUP_FAILED is not a literal top-level assignment"


def test_host_lookup_failed_does_not_collide_with_deploy_sh_or_land_sh_own_codes():
    """Same rule PLAYBOOK_FAILED follows in deploy.sh: pick a code outside every set already
    in play, rather than reusing one that already means something else to a consumer."""
    code = int(re.search(r"^HOST_LOOKUP_FAILED=(\d+)$", _text(), re.MULTILINE).group(1))
    # deploy.sh's own exit codes, and land.sh's own external contract (0/1/2/75) documented
    # in its header.
    reserved = {0, 1, 2, 3, 4, 20, 64, 75}
    assert code not in reserved, (
        f"HOST_LOOKUP_FAILED={code} collides with an existing deploy.sh or land.sh exit code"
    )


def test_deploy_by_host_returns_the_named_constant_not_a_literal():
    line = _host_lookup_line(_text())
    assert '|| return "$HOST_LOOKUP_FAILED"' in line, (
        f"deploy_by_host does not return the named HOST_LOOKUP_FAILED constant: {line!r}"
    )


def _case_arm(text: str, pattern: str) -> str:
    start = text.index('case "$deploy_rc" in')
    block = text[start : text.index("\nesac", start)]
    arm_start = block.index(pattern)
    # Up to the next arm's closing `;;` or the end of the block.
    arm_end = block.index(";;", arm_start) + len(";;")
    return block[arm_start:arm_end]


def test_the_host_lookup_failure_has_its_own_case_arm():
    """A dedicated arm, distinct from the deploy.sh catch-all, so the VERDICT text says
    which side failed rather than printing a bare `exit $deploy_rc`."""
    text = _text()
    arm = _case_arm(text, '"$HOST_LOOKUP_FAILED")')
    assert "deploy_tags.py hosts" in arm, (
        "the HOST_LOOKUP_FAILED case arm does not name what actually failed"
    )
    assert "exit 1" in arm, (
        "the HOST_LOOKUP_FAILED arm must still surface as land.sh's own documented "
        "deploy-failed exit (1), per the header's exit-code table"
    )


def test_removing_the_dedicated_arm_would_be_caught():
    """The reject half: without the arm, the lookup failure falls into the generic
    catch-all, which never names `deploy_tags.py hosts`."""
    text = _text()
    start = text.index('case "$deploy_rc" in')
    block = text[start : text.index("\nesac", start)]
    without_arm = re.sub(
        r'"\$HOST_LOOKUP_FAILED"\)\n(?:.*\n)*?    ;;\n',
        "",
        block,
        count=1,
    )
    assert "deploy_tags.py hosts" not in without_arm.split("*)")[-1], (
        "the mutation did not remove the dedicated arm"
    )


def test_deploy_sh_still_reserves_the_same_colliding_set():
    """Cross-check against deploy.sh's own comment, so the reserved set above can't drift
    silently if deploy.sh ever grows a new exit code."""
    deploy_text = _DEPLOY_SH.read_text()
    assert "{0,1,2,3,4,64,75}" in deploy_text, (
        "deploy.sh's documented colliding set has changed; update the reserved set in "
        "test_host_lookup_failed_does_not_collide_with_deploy_sh_or_land_sh_own_codes "
        "and land.sh's HOST_LOOKUP_FAILED comment to match"
    )
