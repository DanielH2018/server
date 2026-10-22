"""The forward-only flag is set on the real roles the H1 plan names (H1.7b).

`test_promote_forward_only.py` runs the rule over fixture roles, so a real role that omits the flag
passes every case there. H1.5a omitted it for `longhorn`, and the omission surfaced only when a
reviewer read the plan against the tree. This file reads the flag from the committed tree through
the same function `promote.py decide` calls, so a named tag that loses it fails here.

`longhorn-backup` must be forward-only: a reset to a revision before a disarm would re-arm B2 and
restart the transaction-cap spiral (*Forward-only tags and the promote rule*).

Run: uv run pytest scripts/deploy_tools/tests/test_promote_forward_only_real_tags.py
"""

from lib.repo_paths import REPO

from deploy_tools.promote_lib import forward_only

# The tags the plan lists as forward-only, restricted to the roles present in this tree.
NAMED_FORWARD_ONLY = ("longhorn-backup",)


def test_each_named_tag_sets_the_flag_at_head():
    assert set(NAMED_FORWARD_ONLY) <= forward_only.forward_only_roles(REPO, "HEAD")


def test_a_role_that_never_set_the_flag_is_not_forward_only():
    # The red half: the function reports only roles that set it, so an ordinary role is absent.
    assert "bento-pdf" not in forward_only.forward_only_roles(REPO, "HEAD")
