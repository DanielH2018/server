"""Tests for the consumer routing in scripts/secrets_mgmt/consumers.py.

Two mechanisms, tested two ways. The `consumer_tags` cases below pin the name-routing table
against hand-written expectations. The guards after them derive their expectations from the
tree instead — the committed registry, the uptime-kuma tile template and the deploy tag list —
because name routing has already been wrong in production twice and a hand-list would have
agreed with it both times.

Run: uv run pytest scripts/secrets_mgmt/tests/test_secret_consumers.py
"""

from pathlib import Path

from py_table import py_table

from secrets_mgmt.consumers import (
    CROSS_HOST_PUSH_TOKENS,
    FLUX_PATH,
    UPTIME_KUMA_TAG,
    consumer_commands,
    consumer_tags,
)
from secrets_mgmt.secrets_lib.secret_registry import audit
from lib.repo_paths import HOST_VARS
from secrets_mgmt.rotation_tools import REPO, load_registry, today


# ── consumer mapping (which redeploys apply a rotated token) ─────────────────
def test_consumer_tags_monitor_bridge_tokens():
    # The pusher AND the tile. Asserting only the pusher is what let the tile go stale.
    assert consumer_tags("monitor_bridge_cpu_push_token") == (
        "monitor-bridge",
        "uptime-kuma",
    )
    # `kopia_restore_drill_push_token` is absent from both secrets.yml and secret_rotation.yml,
    # so no arm maps it and it resolves to no consumer.
    assert consumer_tags("kopia_restore_drill_push_token") == ()


def test_consumer_tags_non_push_prefixed_token_gets_no_tile():
    """`monitor_bridge_ha_token` carries the prefix for Kuma history but is an HA API token.

    It is the ONE token that resolves a consumer and has no tile, so it is the reject half of
    the `_push_token` discriminator: adding uptime-kuma here would deploy a role that renders
    this name nowhere, which
    `ansible/tests/k8s/test_secret_consumer_census.py::test_no_declared_consumer_tag_names_a_role_that_never_references_the_secret`
    would then fail on.
    """
    assert consumer_tags("monitor_bridge_ha_token") == ("monitor-bridge",)


def test_consumer_tags_cloudflare_ddns_tokens():
    assert consumer_tags("cloudflare_ddns_proxied_push_token") == (
        "cloudflare-ddns",
        "uptime-kuma",
    )


def test_consumer_tags_cross_host_tokens_are_manual():
    # Cross-host / self-referential — the unattended cron must NOT auto-rotate these, and they
    # must NOT pick up the uptime-kuma tag either: their pusher half sits on another HOST, so a
    # tag list would assert a repair one playbook run cannot perform. This is the reject half of
    # the multi-tag change.
    assert consumer_tags("pi_sd_health_push_token") == ()
    assert consumer_tags("pi_recovery_push_token") == ()  # Pi cron, manual Pi deploy
    assert consumer_tags("secret_rotation_push_token") == ()


def test_consumer_tags_autofix_bridge_token():
    # Single-host, single-redeploy auto token — must auto-rotate, not false-skip as cross-host.
    assert consumer_tags("arr_autoblock_push_token") == (
        "autofix-bridge",
        "uptime-kuma",
    )


def test_consumer_tags_registry_gc_token_is_manual():
    # setup/k8s_host_files renders the GC cron's script since registry's D5 conversion, and a
    # setup role has no deploy tag, so the unattended rotation must skip it rather than redeploy
    # `registry`, which no longer renders the token, and leave the cron pushing the old value.
    assert consumer_tags("registry_gc_push_token") == ()


def test_consumer_tags_artifacts_sync_token_is_manual():
    # setup/claude_code renders the sync cron that pushes since H2.7, and a setup role has no
    # deploy tag, so the unattended rotation must skip it rather than redeploy `artifacts`,
    # which no longer renders the token, and leave the cron pushing the old value.
    assert consumer_tags("artifacts_sync_push_token") == ()


def test_every_auto_tier_token_resolves_a_consumer_or_is_known_manual():
    # Registry-driven guard: a new single-host `auto` push token must resolve a consumer_tag
    # (so the unattended weekly `rotate --commit --deploy` cron actually rotates it) or sit in
    # the explicit known-manual allowlist. Without this, a token whose consumer_tag falls
    # through to None silently drops out of rotation and only surfaces months later as an
    # OVERDUE page — exactly how arr_autoblock_push_token slipped in when autofix-bridge landed.
    # Derived from the module's own CROSS_HOST_PUSH_TOKENS (single source — the fifth
    # cross-host token, daniel_box_disk, is when the hand-list here moved there): each entry
    # documents its pusher/label host pair beside the name.
    known_manual = CROSS_HOST_PUSH_TOKENS
    reg = load_registry()
    auto = [n for n, m in reg["entries"].items() if m.get("tier") == "auto"]
    assert auto  # sanity: the registry has auto-tier tokens
    unrotatable = [n for n in auto if not consumer_tags(n) and n not in known_manual]
    assert not unrotatable, (
        "auto-tier tokens with no consumer_tags and not known-manual — they silently drop "
        "out of unattended rotation: %s" % unrotatable
    )


def test_no_cross_host_token_is_badly_overdue():
    # The test above proves each cross-host token is DECLARED manual; this one proves someone is
    # doing the manual part.
    # `consumer_tags` returning () is deliberate and documented beside CROSS_HOST_PUSH_TOKENS:
    # the pusher and the AutoKuma label live on different hosts, so one redeploy cannot update
    # both halves atomically. But the design that skips them assumes an operator picks them up,
    # and the daily audit line that asks reports the whole registry and is easy to skim past.
    #
    # So the reminder becomes a CI failure. This is deliberately NOT the audit's own due-date:
    # the point is to catch sustained neglect, not to fail the build the day something comes
    # due. Rotating one is a manual, two-host procedure — see docs/secret-rotation.md.
    #
    # NOT the fix the reviewer proposed. That was to give CROSS_HOST_PUSH_TOKENS a two-tag
    # consumer list, and it stays rejected — but the REASON is the hosts, not the arity.
    # `consumer_tags` returns two tags for an ordinary push token, because its tile lives in
    # k8s/uptime-kuma while its pusher lives elsewhere, and BOTH deploy from daniel-box in one
    # playbook run. CROSS_HOST tokens are the case that remains unrepresentable: their
    # two halves sit on different HOSTS, so any tag list would assert a repair that no single
    # `rotate --deploy` can perform. They still return `()`, and
    # test_consumer_tags_cross_host_tokens_are_manual is the guard that keeps them there.
    grace_days = 30
    reg = load_registry()
    res = audit(reg, today())
    badly_overdue = [
        (name, -days)
        for name, _tier, _due, days in res["all"]
        if name in CROSS_HOST_PUSH_TOKENS and days < -grace_days
    ]
    assert not badly_overdue, (
        f"cross-host push tokens more than {grace_days} days overdue: {badly_overdue}. These "
        f"are skipped by the unattended weekly cron BY DESIGN, so nothing rotates them but a "
        f"person. Rotate them (docs/secret-rotation.md), then `uv run python "
        f"scripts/secrets_mgmt/secret_rotation.py sync` and commit."
    )


# ── consumer_tag correctness ────────────────────────────────────────────────
# The guard above proves a token RESOLVES a tag. Nothing proved the tag was right, which is
# this estate's recurring guard-scope shape: a check written alongside a fix inherits the
# fix's scope. Routing every `monitor_bridge_*` name to monitor-bridge by prefix is wrong,
# because nine of those tokens are pushed from another role's health script entirely —
# the prefix is a Kuma monitor-history artefact, kept so renaming the monitor would not lose
# its history. For those, `rotate --deploy` would write a new value, deploy a role that renders
# the token nowhere, leave the real pusher on the old one, and stamp `last_rotated` green.
# `ansible/tests/k8s/test_secret_consumer_census.py` proves each tag's role references the
# token, measured by `tree_consumers`, which also credits a role that renders it out of another
# role's file (#3781).
_SKIP_TAGS = ("ignore", "pinned", "external")


def _consumer_tags():
    reg = load_registry()
    for name in reg["entries"]:
        for tag in consumer_tags(name):
            if tag not in _SKIP_TAGS:
                yield name, tag


def test_every_consumer_tag_is_a_real_deploy_tag():
    """The same failure one step along: every consumer tag must be a real deploy tag.

    `ansible-playbook deploy.yml --tags <unmatched>` runs nothing and exits 0, so the rotation still
    reads as deployed. Three mis-routed tokens are pushed by SETUP roles with no `containers_list`
    entry, which is why they decline in CROSS_HOST_PUSH_TOKENS instead of naming their role.
    """
    from deploy_tools.deploy_lib import tags as deploy_tags

    valid = deploy_tags.known_tags()
    assert valid, "could not read the deploy tag list"
    unmatched = sorted(
        "%s -> %s" % (n, t) for n, t in _consumer_tags() if t not in valid
    )
    assert not unmatched, (
        "consumer_tag returns a value that is not a deploy tag; Ansible exits 0 on an "
        "unmatched tag, so the rotation stamps green having deployed nothing: %s"
        % unmatched
    )


def test_uptime_kuma_is_a_consumer_iff_a_tile_exists():
    """Derive the tile half of the consumer list from the template, never from a hand-list.

    `consumer_tags` decides by name (`_push_token`), which is a proxy. The ground truth is
    whether k8s/uptime-kuma's static-monitors template actually renders the token, and the two
    must agree in BOTH directions:

      - a token WITH a tile that omits `uptime-kuma` rotates the pusher only, so AutoKuma keeps
        reconciling the old push_token and the monitor stops beating — the bug this change fixes;
      - a token WITHOUT a tile that claims `uptime-kuma` deploys a role rendering it nowhere,
        which is the mis-routing the 2026-08-25 M-8b finding was about, one role along.

    Measured: 56 tokens have a tile, 43 resolve a consumer, 42 are in both, and the lone
    `monitor_bridge_ha_token` sits outside. The 14 tile-bearing tokens that resolve NOTHING are
    the cross-host ones — deliberately manual, and this test must not drag them back in.
    """
    tile = (
        Path(REPO) / "ansible/roles/k8s/uptime-kuma/templates/static-monitors.yaml.j2"
    ).read_text()
    # The bridge's tiles render from one loop over its check table (#3781), so their tokens
    # are the table's rather than literals in the template.
    bridge_tokens = {
        row["token"]
        for row in py_table(
            (
                Path(REPO) / "ansible/roles/k8s/monitor-bridge/files/check_table.py"
            ).read_text(),
            "CHECKS",
        )
    }
    assert "monitor_bridge_traefik_421_push_token" in bridge_tokens
    reg = load_registry()
    wrong = []
    for name in reg["entries"]:
        tags = consumer_tags(name)
        if not tags:
            continue  # manual: this test says nothing about tokens with no consumer at all
        has_tile = ("{{ %s }}" % name) in tile or name in bridge_tokens
        claims_kuma = UPTIME_KUMA_TAG in tags
        if has_tile != claims_kuma:
            wrong.append("%s: tile=%s but consumer_tags=%s" % (name, has_tile, tags))
    assert not wrong, (
        "consumer_tags disagrees with the rendered tile — each of these either rotates into a "
        "stale Kuma monitor or deploys a role that renders the token nowhere: %s"
        % wrong
    )


def test_the_tile_and_pusher_tags_are_both_real_deploy_targets():
    """Both halves must be deployable in ONE run, which is what separates this from cross-host.

    If `uptime-kuma` were not a valid deploy tag, `rotate --deploy` would exit 2 (the wrapper's
    unmatched-tag guard) and rotate nothing — a fix that breaks the thing it fixes.
    """
    host_vars = (HOST_VARS / "daniel-box.yml").read_text()
    assert "name: uptime-kuma" in host_vars, (
        "uptime-kuma must be in daniel-box's containers_list for the tag to match anything"
    )
    assert "name: monitor-bridge" in host_vars


# ── consumer_commands leaves a Flux-owned tag off the deploy.sh line (#4983) ──
# `deploy.sh` exits 5 on a tag whose containers_list entry carries `owner`, and exit 5 refuses
# the WHOLE run, so one owned consumer on the printed line deploys none of the others. The
# fixture is a whole repo, `ansible/` and all, because `consumer_commands` takes `repo` and the
# owner it reads must come from that repo's containers_list rather than the live tree's.
_FIXTURE_SECRET = "fixture_issue_4983_api_key"


def _repo_with_a_flux_owned_consumer(tmp_path: Path, owner: str | None = "flux") -> str:
    """A repo whose secret is rendered by `bento-pdf` (owner: `owner`) and `sonarr` (unowned)."""
    for role in ("bento-pdf", "sonarr"):
        template = tmp_path / "ansible/roles/k8s" / role / "templates/secret.yaml.j2"
        template.parent.mkdir(parents=True)
        template.write_text(f"apiKey: {{{{ {_FIXTURE_SECRET} }}}}\n")
    host_vars = tmp_path / "ansible/inventory/host_vars/daniel-box.yml"
    host_vars.parent.mkdir(parents=True)
    host_vars.write_text(
        "containers_list:\n"
        "  - name: bento-pdf\n"
        "    platform: k8s\n"
        + (f"    owner: {owner}\n" if owner else "")
        + "  - name: sonarr\n"
        "    platform: k8s\n"
    )
    return str(tmp_path)


def _deploy_lines(commands: list[str]) -> list[str]:
    return [c for c in commands if "deploy.sh" in c]


def test_consumer_commands_prints_no_deploy_line_naming_a_flux_owned_tag(tmp_path):
    commands = consumer_commands(
        _FIXTURE_SECRET, repo=_repo_with_a_flux_owned_consumer(tmp_path)
    )

    naming_owned = [c for c in _deploy_lines(commands) if "bento-pdf" in c]
    assert naming_owned == [], (
        f"deploy.sh exits 5 on the Flux-owned bento-pdf tag and so deploys nothing: {commands}"
    )


def test_consumer_commands_keeps_the_unowned_consumers_on_one_deploy_line(tmp_path):
    commands = consumer_commands(
        _FIXTURE_SECRET, repo=_repo_with_a_flux_owned_consumer(tmp_path)
    )

    assert _deploy_lines(commands) == ['`./scripts/deploy.sh --tags "sonarr"`'], (
        commands
    )


def test_consumer_commands_prints_one_line_naming_the_owner_of_each_dropped_tag(
    tmp_path,
):
    commands = consumer_commands(
        _FIXTURE_SECRET, repo=_repo_with_a_flux_owned_consumer(tmp_path)
    )

    owner_lines = [
        c for c in commands if "deploy.sh" not in c and "bento-pdf" in c and "flux" in c
    ]
    assert len(owner_lines) == 1, commands


# ── the Flux path for a Flux-owned consumer (D3.4) ──
# d3-secrets.md, *Rotating a value read by both planes*: Flux substitutes the value from the
# cluster file, so the line for an `owner: flux` consumer names the operator's `write` and the
# landing, and an Ansible-owned consumer keeps its `deploy.sh` line.


def test_consumer_commands_prints_the_flux_path_for_a_flux_owned_consumer(tmp_path):
    commands = consumer_commands(
        _FIXTURE_SECRET, repo=_repo_with_a_flux_owned_consumer(tmp_path)
    )

    assert commands == [
        '`./scripts/deploy.sh --tags "sonarr"`',
        FLUX_PATH % "bento-pdf",
    ]
    assert "`uv run python scripts/secrets_mgmt/cluster_vars.py write`" in commands[1]


def test_consumer_commands_for_ansible_owned_consumers_prints_one_deploy_line(tmp_path):
    commands = consumer_commands(
        _FIXTURE_SECRET, repo=_repo_with_a_flux_owned_consumer(tmp_path, owner=None)
    )

    assert commands == ['`./scripts/deploy.sh --tags "bento-pdf,sonarr"`']
