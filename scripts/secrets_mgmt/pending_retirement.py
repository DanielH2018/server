"""Keys whose retirement is scheduled but not yet due.

A leaf: it imports nothing, so both `consumers.py` and `cluster_vars_check.py` can read it
without a cycle.
"""

# Push tokens whose checks M5's PR 4a deleted, but whose keys stay in secrets.yml, the rotation
# registry and the cluster file for a week. Operator ruling, 2026-10-22: a token retirement lands
# a week after the cutover that frees it, because a revert of the cutover needs the token and a
# retired key cannot come back by revert. Each name has no consumer until then:
# `consumers.consumer_tags` returns nothing for it, and `cluster_vars_check.names_problems`
# does not demand a cluster render for it. M5's PR 5 (PR_5_URL) retires the keys, not before the
# `monitor-bridge` deploy of PR 4a plus 7 days, and deletes this set in the same commit.
PENDING_RETIREMENT: frozenset[str] = frozenset(
    {
        "monitor_bridge_kuma_notify_failures_push_token",
        "monitor_bridge_loki_push_token",
        "monitor_bridge_loki_reachable_push_token",
    }
)
