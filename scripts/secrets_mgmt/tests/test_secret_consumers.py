"""Tests for the consumer routing in scripts/secrets_mgmt/consumers.py.

Two mechanisms, tested two ways. The `consumer_tags` cases below pin the name-routing table
against hand-written expectations. The guards after them derive their expectations from the
tree instead — the committed registry, the uptime-kuma tile template and the deploy tag list —
because name routing has already been wrong in production twice and a hand-list would have
agreed with it both times.

Run: uv run pytest scripts/secrets_mgmt/tests/test_secret_consumers.py
"""

from pathlib import Path

from secrets_mgmt.consumers import (
    CROSS_HOST_PUSH_TOKENS,
    UPTIME_KUMA_TAG,
    consumer_tags,
)
from secrets_mgmt.secret_registry import audit
from secrets_mgmt.rotation_tools import REPO, load_registry, today


# ── consumer mapping (which redeploys apply a rotated token) ─────────────────
def test_consumer_tags_monitor_bridge_tokens():
    # The pusher AND the tile. Asserting only the pusher is what let the tile go stale.
    assert consumer_tags("monitor_bridge_cpu_push_token") == (
        "monitor-bridge",
        "uptime-kuma",
    )
    # `kopia_restore_drill_push_token` had its own arm in the same branch until 2026-08-27. It is
    # absent from both secrets.yml and secret_rotation.yml, so the arm mapped a name that could
    # never be passed and the assertion here was the only thing keeping it alive.
    assert consumer_tags("kopia_restore_drill_push_token") == ()


def test_consumer_tags_non_push_prefixed_token_gets_no_tile():
    """`monitor_bridge_ha_token` carries the prefix for Kuma history but is an HA API token.

    It is the ONE token that resolves a consumer and has no tile, so it is the reject half of
    the `_push_token` discriminator: adding uptime-kuma here would deploy a role that renders
    this name nowhere, which `test_every_consumer_tag_names_a_role_that_renders_the_token`
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
    # 2026-08-24 review M-3, second run of the same finding. The test above proves each
    # cross-host token is DECLARED manual; nothing proved anyone was doing the manual part.
    # `consumer_tags` returning () is deliberate and documented beside CROSS_HOST_PUSH_TOKENS:
    # the pusher and the AutoKuma label live on different hosts, so one redeploy cannot update
    # both halves atomically. But the design that skips them assumes an operator picks them up,
    # and the only thing asking was the daily audit line — which reports the whole registry and
    # is easy to skim past. Two consecutive reviews found the same tokens unrotated.
    #
    # So the reminder becomes a CI failure. This is deliberately NOT the audit's own due-date:
    # the point is to catch sustained neglect, not to fail the build the day something comes
    # due. Rotating one is a manual, two-host procedure — see docs/secret-rotation.md.
    #
    # NOT the fix the reviewer proposed. That was to give CROSS_HOST_PUSH_TOKENS a two-tag
    # consumer list, and it stays rejected — but the REASON is the hosts, not the arity.
    # `consumer_tags` became genuinely multi-valued on 2026-08-28, because a push token's tile
    # lives in k8s/uptime-kuma while its pusher lives elsewhere, and BOTH deploy from daniel-box
    # in one playbook run. CROSS_HOST tokens are the case that remains unrepresentable: their
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
# fix's scope. `consumer_tag` routed every `monitor_bridge_*` name to monitor-bridge by
# prefix, but nine of those tokens are pushed from another role's health script entirely —
# the prefix is a Kuma monitor-history artefact, kept so renaming the monitor would not lose
# its history. For those, `rotate --deploy` wrote a new value, deployed a role that renders
# the token nowhere, left the real pusher on the old one, and stamped `last_rotated` green
# (2026-08-25 review M-8b).
_SKIP_TAGS = ("ignore", "pinned", "external")


def _consumer_tags():
    reg = load_registry()
    for name in reg["entries"]:
        for tag in consumer_tags(name):
            if tag not in _SKIP_TAGS:
                yield name, tag


def test_every_consumer_tag_names_a_role_that_renders_the_token():
    roles = Path(REPO) / "ansible/roles"
    mismatched = []
    for name, tag in _consumer_tags():
        candidates = [p for p in roles.glob("*/" + tag) if p.is_dir()]
        if not candidates:
            mismatched.append("%s -> %s (no such role directory)" % (name, tag))
            continue
        renders = any(
            name in f.read_text(errors="ignore")
            for role in candidates
            for f in role.rglob("*")
            if f.is_file() and f.suffix in (".j2", ".yml", ".yaml", ".sh", ".py")
        )
        if not renders:
            mismatched.append("%s -> %s (role renders it nowhere)" % (name, tag))
    assert not mismatched, (
        "consumer_tag names a role that does not render the token, so `rotate --deploy` "
        "deploys the wrong thing and stamps the rotation green anyway: %s" % mismatched
    )


def test_every_consumer_tag_is_a_real_deploy_tag():
    """The same failure one step along: every consumer tag must be a real deploy tag.

    `ansible-playbook deploy.yml --tags <unmatched>` runs nothing and exits 0, so the rotation still
    reads as deployed. Three mis-routed tokens are pushed by SETUP roles with no `containers_list`
    entry, which is why they decline in CROSS_HOST_PUSH_TOKENS instead of naming their role.
    """
    import deploy_tags

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

    Measured 2026-08-28: 56 tokens have a tile, 43 resolve a consumer, 42 are in both, and the
    lone `monitor_bridge_ha_token` sits outside. The 14 tile-bearing tokens that resolve NOTHING
    are the cross-host ones — deliberately manual, and this test must not drag them back in.
    """
    tile = (
        Path(REPO) / "ansible/roles/k8s/uptime-kuma/templates/static-monitors.yaml.j2"
    ).read_text()
    reg = load_registry()
    wrong = []
    for name in reg["entries"]:
        tags = consumer_tags(name)
        if not tags:
            continue  # manual: this test says nothing about tokens with no consumer at all
        has_tile = ("{{ %s }}" % name) in tile
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
    host_vars = (Path(REPO) / "ansible/inventory/host_vars/daniel-box.yml").read_text()
    assert "name: uptime-kuma" in host_vars, (
        "uptime-kuma must be in daniel-box's containers_list for the tag to match anything"
    )
    assert "name: monitor-bridge" in host_vars
