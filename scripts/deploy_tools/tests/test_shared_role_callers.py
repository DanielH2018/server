"""`shared_role_callers.py`: which tags' deploys run a shared role."""

import shared_role_callers


# ── which tags deploy a shared role at all, record or no record ──
def test_caller_tags_follows_a_shared_caller_and_skips_a_dead_end():
    """A first-hop-only walk would miss `web`, so `longhorn-api` could never discharge."""
    callers = {"longhorn-api": {"volume-snapshot", "dead"}, "volume-snapshot": {"web"}}
    assert shared_role_callers.caller_tags("longhorn-api", {"web"}, callers) == {"web"}


def test_expand_shared_tags_keeps_order_and_leaves_an_unknown_name_as_typed():
    tags, replaced, _ = shared_role_callers.expand_shared_tags(
        ["web", "helper", "typo"], {"web", "api"}, {"helper": {"api", "web"}}
    )
    assert tags == ["web", "api", "typo"]
    assert replaced == {"helper": ["api", "web"]}
