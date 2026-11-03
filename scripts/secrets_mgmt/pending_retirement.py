"""Keys whose retirement is scheduled but not yet due.

A leaf: it imports nothing, so both `consumers.py` and `cluster_vars_check.py` can read it
without a cycle.
"""

# Push tokens whose checks a cutover deleted, but whose keys stay in secrets.yml, the rotation
# registry and the cluster file for a week. Operator ruling, 2026-10-22: a token retirement lands
# a week after the cutover that frees it, because a revert of the cutover needs the token and a
# retired key cannot come back by revert. Each name has no cluster render until then:
# `cluster_vars_check.names_problems` does not demand one for it, and `cluster_vars.write` keeps
# it in the cluster file (#5960).
# M5's PR 5 retired its three tokens on 2026-10-29, M6's Phase A and B token PRs retired theirs
# a week after each batch's cutover, and M3's PR 6 retired `daniel_box_disk_push_token` on
# 2026-11-02, and B6c's token PR retired `registry_gc_push_token` the same day and deleted this
# module. M6 batch B9's Cutover-1a brings it back for the six integrity-scan tokens. B9's
# token PR, the last retirement the plans schedule, deletes it again with its readers and tests.
PENDING_RETIREMENT: frozenset[str] = frozenset(
    {
        # M6 batch B9's Cutover-1a deleted the three `AIDE Integrity` and three `rkhunter Scan`
        # tiles, so no cluster render references these tokens. The scans still push into them
        # until Cutover-2, and each host package still carries its token as a credential until
        # B9's token PR removes it. That PR retires the keys a week after Cutover-1b: the operator
        # removes them with `sops` and runs `cluster_vars.py write`, then the PR deletes these
        # entries.
        "aide_daniel_box_push_token",
        "aide_daniel_pi_push_token",
        "aide_daniel_server_push_token",
        "rkhunter_daniel_box_push_token",
        "rkhunter_daniel_pi_push_token",
        "rkhunter_daniel_server_push_token",
    }
)
