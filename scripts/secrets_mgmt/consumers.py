"""Who holds a copy of a secret, by two deliberately different mechanisms.

`consumer_tags()` routes by NAME. It is fast, it covers the push-token fleet, and it is what
the unattended weekly rotation acts on: a token with no tags is MANUAL and the cron skips it.

`tree_consumers()` measures the same question from the tree with a grep. It answers "who is
still holding the old value?" for every secret, including the ones name-routing has no rule
for — the question a rotation actually poses, and the one `audit` does not answer.

Both are pure reads: nothing here decrypts, writes or shells out.
"""

from __future__ import annotations

import os

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # scripts/

from secrets_mgmt.rotation_tools import REPO

# Push tokens whose pusher and AutoKuma `push_token` label live on DIFFERENT hosts, or which
# reference the rotation tool itself — one redeploy cannot update both halves atomically, so these
# stay MANUAL: consumer_tags returns (), the unattended cron skips them, the audit still reminds.
# The single source the guard test derives its allowlist from; a new cross-host token is added
# HERE, with its host pair, not to the test.
CROSS_HOST_PUSH_TOKENS = frozenset(
    {
        "pi_sd_health_push_token",  # Pi cron + daniel-server label
        "pi_recovery_push_token",  # Pi cron + daniel-server label
        "longhorn_backup_push_token",  # daniel-box cron (k3s role) + daniel-server label
        "claude_otel_push_token",  # daniel-box cron (k8s/claude-otel) + daniel-server label
        "daniel_box_disk_push_token",  # daniel-box cron (k3s role) + daniel-server label
        "manifest_prune_push_token",  # daniel-box cron (k3s role) + k8s/uptime-kuma static tile
        "release_staleness_push_token",  # daniel-box cron (k3s role) + k8s/uptime-kuma static tile
        "live_drift_push_token",  # daniel-box cron (k3s role) + k8s/uptime-kuma static tile
        "etcd_snapshot_push_token",  # daniel-box cron (k3s role) + k8s/uptime-kuma static tile
        "remember_logs_push_token",  # daniel-box cron (k3s role) + k8s/uptime-kuma static tile
        # daniel-box cron (setup/gitops_deploy) + k8s/uptime-kuma static tile. Declines for the
        # same reason the setup-plane tokens below do rather than naming a role: gitops_deploy has
        # no `containers_list` entry, so `deploy.yml --tags gitops_deploy` matches nothing and
        # exits 0 — a rotation would stamp green having deployed neither half. It is applied by
        # `initial_setup.yml --tags gitops_deploy`, by hand, because it is the role running the
        # tick.
        "ruleset_drift_push_token",
        # daniel-box cron (setup/initial_setup) + k8s/uptime-kuma static tile. Both halves embed
        # the token and must move together; initial_setup has no deploy tag, so a `--deploy
        # --tags uptime-kuma` would rotate the tile and leave the cron pushing the old value —
        # which reads as the monitor going silent, the exact fault it was added to report.
        "docs_refresh_push_token",
        # daniel-SERVER cron (setup/initial_setup) + a k8s/uptime-kuma tile deployed from
        # daniel-box: two hosts, so no single redeploy can move both halves. Rotating the tile
        # alone would leave the cron pushing the old value, silencing the only monitor that can
        # report a stale render on the host that owns the UPS shutdown chain.
        "setup_drift_push_token",
        "secret_rotation_push_token",  # self-referential
        # Pushed by a setup role with no deploy tag, so there is nothing for --deploy to run.
        # Named `monitor_bridge_*` only for Kuma monitor-history continuity after the check
        # moved out of monitor-bridge (2026-08-25 review M-8b).
        "monitor_bridge_fake_remux_push_token",  # setup/fake_remux cron
        "monitor_bridge_fake_remux_replace_push_token",  # setup/fake_remux cron
        "monitor_bridge_renovate_alive_push_token",  # setup/renovate_notify
        "renovate_agent_kuma_push_token",  # setup/renovate_agent, same shape
        # Same reason as its two fake_remux siblings above: pushed by a setup role with no deploy
        # tag, so there is nothing for --deploy to run. The tile is in k8s/uptime-kuma.
        "mkv_attachment_repair_push_token",
        # nut_host cron (renders /etc/nut/kuma-push.env) + k8s/uptime-kuma static tile. Same shape
        # as docs_refresh_push_token above: nut_host runs only from initial_setup.yml and has no
        # deploy tag, so `--deploy --tags uptime-kuma` would move the tile and leave the root cron
        # pushing the old value — silencing the monitor that watches the shutdown chain.
        "ups_secondary_push_token",
        # daniel-server's (ups_host) own leg of the same watchdog, added in issue #952 so the
        # two hosts stop sharing ups_secondary_push_token above — same shape and same reason.
        "ups_secondary_daniel_server_push_token",
    }
)


# Push tokens whose name carries the `monitor_bridge_` prefix but whose PUSHER lives in another
# role entirely. The prefix is a Kuma-history artefact: the monitor was created by monitor-bridge
# and renaming it would break its history, so the token kept the name after the check moved out
# into the owning service's own health script. Routing these by prefix names a role that renders
# them NOWHERE — `rotate --deploy` would write a new value, deploy monitor-bridge, leave the real
# pusher on the old token and stamp `last_rotated` green (2026-08-25 review M-8b).
#
# Derived by measurement, not by reading the names: `grep -rl <token> ansible/roles/`. Nine of
# the 41 `monitor_bridge_*` tokens mis-routed; the review reported two.
PREFIX_EXCEPTION_CONSUMERS = {
    "monitor_bridge_appsec_push_token": "crowdsec",
    "monitor_bridge_home_allowlist_push_token": "crowdsec",
    "monitor_bridge_cloudflare_drift_push_token": "traefik",
    "monitor_bridge_configarr_push_token": "configarr",
    "monitor_bridge_janitorr_push_token": "janitorr",
    "monitor_bridge_pi_peers_push_token": "pi-peer-backup",
}
# The other three mis-routed tokens are pushed by SETUP roles (setup/fake_remux,
# setup/renovate_notify), which have no entry in `containers_list` and therefore no deploy tag.
# Naming the role here would be the same defect one step along: `ansible-playbook deploy.yml
# --tags fake_remux` matches nothing and Ansible exits 0, so the rotation would still stamp
# green having deployed nothing. They decline instead, above.


# Every push token has TWO consumers in the cluster, and until 2026-08-28 this function named
# only one of them. The pusher reads it from its own role's env Secret; the Kuma monitor that
# receives the push is a static AutoKuma entity rendered by k8s/uptime-kuma
# (`static-monitors.yaml.j2`, a manifests_secret_file). AutoKuma reconciles the live monitor's
# `push_token` FROM that Secret, so a rotation that redeploys only the pusher leaves Kuma
# expecting the old token: the bridge then pushes a token nothing matches, the monitor stops
# beating, and it goes DOWN. That is loud rather than silent, but it is a self-inflicted outage
# on every rotated push monitor, and `rotate --deploy` stamped `last_rotated` green through it.
#
# Measured 2026-08-28 against the live registry and template: 43 tokens resolve a consumer,
# 42 of them have a tile, and the single exception is `monitor_bridge_ha_token` — an HA API
# token that carries the prefix for Kuma history reasons but is not a push token at all. So
# `_push_token` is the exact discriminator, and `test_uptime_kuma_is_a_consumer_iff_a_tile_
# exists` derives the split from the template rather than trusting this comment.
UPTIME_KUMA_TAG = "uptime-kuma"


def consumer_tags(name: str) -> tuple[str, ...]:
    """Deploy tags whose redeploy makes a rotated push token take effect.

    EMPTY when the consumer spans hosts or is self-referential — those stay MANUAL: the
    unattended cron skips them, the audit still reminds. Plural, and a tuple, since
    2026-08-28. The pre-migration docstring here said a push token
    "lives in two places on one compose file", which was true under Docker+AutoKuma labels and
    false after the k3s migration split the pusher and the tile into two roles. Both roles
    deploy from daniel-box in ONE playbook run, so both tags are reachable by a single
    `rotate --deploy` — which is exactly what distinguishes this from CROSS_HOST_PUSH_TOKENS,
    where the two halves sit on different HOSTS and no redeploy can cover them. Those still
    return empty; a multi-tag return there would assert a repair that cannot happen.
    """
    # Both of these precede the prefix rule below: every token they name also carries the
    # `monitor_bridge_` prefix, so the prefix rule would otherwise claim them first.
    if name in CROSS_HOST_PUSH_TOKENS:
        return ()
    elif name == "kuma_status_page_sync_push_token":
        # The one token whose pusher and tile are the SAME role: the status-page-sync CronJob
        # reads it from autokuma-credentials, and its tile is a static-file entity beside it.
        # Returning the tag twice would be the honest reading of the rule below, and useless.
        return (UPTIME_KUMA_TAG,)
    elif name in PREFIX_EXCEPTION_CONSUMERS:
        pusher: str | None = PREFIX_EXCEPTION_CONSUMERS[name]
    elif name.startswith("monitor_bridge_"):
        pusher = "monitor-bridge"
    elif name.startswith("cloudflare_ddns_"):
        pusher = "cloudflare-ddns"
    elif name == "docker_fleet_push_token":
        # The monitor-bridge role renders the host cron script; the monitor itself is a
        # static-file entity in the cluster Kuma (slice-7 Phase D KD2).
        pusher = "monitor-bridge"
    elif name == "arr_autoblock_push_token":
        # autofix-bridge (daniel-server only) renders the pusher's env. (Token name kept as
        # arr_autoblock_* through the arr-autoblock -> autofix-bridge rename for Kuma history
        # continuity; the consumer is the autofix-bridge deploy tag.)
        pusher = "autofix-bridge"
    else:
        # anything else unrecognised -> manual
        return ()
    if name.endswith("_push_token"):
        return (pusher, UPTIME_KUMA_TAG)
    return (pusher,)


# Planes a role can live on, and what redeploying it actually costs the operator. The
# distinction is not cosmetic: `deploy.sh` derives its valid tags from `containers_list`, so a
# setup-plane role HAS no deploy tag and `deploy.sh --tags fake_remux` exits 2 having deployed
# nothing (CLAUDE.md, Common Commands). That is the trap this census exists to surface.
_ROLE_PLANES = {
    "k8s": "deploy",
    "containers": "deploy",
    "setup": "setup",
}

# Directories whose hits are not consumers. `archive/` is retired code; `collections/` is
# vendored third-party; the registry and secrets file name every secret by definition, so
# including them would make every census non-empty and the check vacuous.
_CENSUS_SKIP = (
    os.path.join("roles", "containers", "archive"),
    "collections",
)
_CENSUS_SKIP_FILES = {"secrets.yml", "secret_rotation.yml"}

# Markdown is EXCLUDED, and this is a correctness fix rather than tidiness. A role's CLAUDE.md
# names secrets it explains without rendering any of them, so counting docs makes the census
# claim consumers that hold no copy — and sends the operator to redeploy a role that cannot
# help. Measured 2026-08-29 across all 149 secrets: 5 secrets gained 11 doc-only roles, and
# `r2_access_key_id` is the clearest — monitor-bridge's CLAUDE.md discusses it while the only
# renderers are setup/k3s's longhorn-r2-secret.yaml.j2 and health-crons.yml.
#
# Checked in the direction that matters before making the change: excluding docs creates ZERO
# new phantom tags, so no real consumer is only discoverable through prose.
_CENSUS_SKIP_SUFFIXES = (".md",)


def tree_consumers(name: str, repo: str = REPO) -> dict[str, str]:
    """Every role that REFERENCES this secret, measured from the tree — role -> plane.

    The counterpart to `consumer_tags()` above, and deliberately a different mechanism.
    `consumer_tags()` routes by NAME, which is fast and covers the push-token fleet, but its
    own comment records what name-routing costs: nine of 41 `monitor_bridge_*` tokens named a
    role that renders them nowhere, and the fix was measured with `grep -rl <token>
    ansible/roles/` rather than reasoned from the prefix. This does that grep in code so the
    measurement is repeatable and can be asserted against.

    It answers the question a rotation actually poses — "who now holds a stale copy?" — which
    `consumer_tags()` cannot for anything outside its table. `sonarr_api_key` falls to that
    function's default and returns `()`, meaning MANUAL: correct, but it names nobody, and on
    2026-08-29 that left seven consumers holding a dead key for ~40 minutes.

    Returns a plane per role because the repair command differs by plane and one of them is
    unreachable from `deploy.sh` — see `_ROLE_PLANES`.
    """
    found: dict[str, str] = {}
    ansible_dir = os.path.join(repo, "ansible")
    for dirpath, dirnames, filenames in os.walk(ansible_dir):
        rel = os.path.relpath(dirpath, ansible_dir)
        if any(skip in rel for skip in _CENSUS_SKIP):
            dirnames[:] = []
            continue
        for filename in filenames:
            if filename in _CENSUS_SKIP_FILES or filename.endswith(
                _CENSUS_SKIP_SUFFIXES
            ):
                continue
            path = os.path.join(dirpath, filename)
            try:
                with open(path, encoding="utf-8", errors="ignore") as handle:
                    text = handle.read()
            except OSError:
                # A file this process cannot read is not evidence of absence, but it is also
                # not something a census can act on. Skipping is right; failing the whole
                # census on one unreadable file would make the tool useless in a worktree.
                continue
            if name not in text:
                continue
            parts = os.path.relpath(path, ansible_dir).split(os.sep)
            if len(parts) >= 3 and parts[0] == "roles" and parts[1] in _ROLE_PLANES:
                found[parts[2]] = _ROLE_PLANES[parts[1]]
    return found


def consumer_commands(name: str, repo: str = REPO) -> list[str]:
    """The exact commands that make a rotated `name` take effect everywhere, deploy plane first.

    Setup-plane roles are listed with their own playbook because `deploy.sh` cannot reach them
    and exits 2 rather than failing loudly at the role.
    """
    consumers = tree_consumers(name, repo=repo)
    deploy_tags = sorted(r for r, plane in consumers.items() if plane == "deploy")
    setup_roles = sorted(r for r, plane in consumers.items() if plane == "setup")
    # BACKTICKS ARE LOAD-BEARING, not decoration. This module is reached by two crons, and
    # `reference/scripts.py` classifies a script as `scheduled` when a scheduled script's
    # text invokes it — so a bare `./scripts/deploy.sh ...` literal here would reclassify the
    # interactive deploy path as cron-driven in the generated reference. That resolver already
    # draws the distinction this needs (`_invoked_in`, reference/scripts.py:102): a line
    # carrying a backtick is prose CITING a command rather than running one, which is exactly
    # what these strings are. Quoting them says so in the one way the tree can read.
    commands = []
    if deploy_tags:
        commands.append('`./scripts/deploy.sh --tags "%s"`' % ",".join(deploy_tags))
    for role in setup_roles:
        commands.append(
            "`uv run ansible-playbook ansible/initial_setup.yml --tags %s`" % role
        )
    return commands
