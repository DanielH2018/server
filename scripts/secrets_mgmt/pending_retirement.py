"""Keys whose retirement is scheduled but not yet due.

A leaf: it imports nothing, so both `consumers.py` and `cluster_vars_check.py` can read it
without a cycle.
"""

# Push tokens whose checks a cutover deleted, but whose keys stay in secrets.yml, the rotation
# registry and the cluster file for a week. Operator ruling, 2026-10-22: a token retirement lands
# a week after the cutover that frees it, because a revert of the cutover needs the token and a
# retired key cannot come back by revert. Each name has no consumer until then:
# `consumers.consumer_tags` returns nothing for it, `cluster_vars_check.names_problems` does not
# demand a cluster render for it, and `cluster_vars.write` keeps it in the cluster file (#5960).
# M5's PR 5 retired its three tokens on 2026-10-29, M6's Phase A and B token PRs retired theirs
# a week after each batch's cutover, and M3's PR 6 retired `daniel_box_disk_push_token` on
# 2026-11-02. B6c's token PR, the last retirement the plans schedule, deletes this module with
# its readers and tests.
PENDING_RETIREMENT: frozenset[str] = frozenset(
    {
        # M6 batch B6c's Cutover-1a deleted the `Registry GC` tile, and no cluster render
        # references the token. Cutover-2 dropped registry-gc.sh's push, so no render of any kind
        # does. B6c's token PR retires the key a week after Cutover-1b and deletes this entry
        # with it.
        "registry_gc_push_token",
    }
)
