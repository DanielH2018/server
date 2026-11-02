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
# 2026-11-02, and B6c's token PR retired `registry_gc_push_token` the same day and deleted this
# module. M6 batch A6's Cutover-2 brings it back for the bridge's 20 tokens, and A6-9, the last
# retirement the plans schedule, deletes it again with its readers and tests.
PENDING_RETIREMENT: frozenset[str] = frozenset(
    {
        # M6 batch A6's Cutover-1a deleted the 20 monitor-bridge tiles and its Cutover-2 stopped
        # the push and dropped the `KUMA_PUSH_*` keys from the env Secret, so no cluster render
        # references these tokens. The `token` fields of `check_table.py` still spell them until
        # A6-8, which is source text and not a render. A6-9 retires the keys a week after
        # Cutover-1b: the operator removes them with `sops` and runs `cluster_vars.py write`,
        # then the PR deletes these entries.
        "monitor_bridge_arr_queue_push_token",
        "monitor_bridge_b2_reachable_push_token",
        "monitor_bridge_b2_storage_push_token",
        "monitor_bridge_bazarr_push_token",
        "monitor_bridge_cloudflare_drift_push_token",
        "monitor_bridge_discord_push_token",
        "monitor_bridge_etcd_drill_push_token",
        "monitor_bridge_gitops_alive_push_token",
        "monitor_bridge_gitops_status_push_token",
        "monitor_bridge_ha_push_token",
        "monitor_bridge_healthchecks_drift_push_token",
        "monitor_bridge_host_packages_push_token",
        "monitor_bridge_n8n_push_token",
        "monitor_bridge_prometheus_push_token",
        "monitor_bridge_prowlarr_indexers_push_token",
        "monitor_bridge_r2_usage_push_token",
        "monitor_bridge_scrutiny_push_token",
        "monitor_bridge_speedtest_push_token",
        "monitor_bridge_swallowed_verdicts_push_token",
        "monitor_bridge_wan_reachable_push_token",
    }
)
