"""Tests for the consumer routing in scripts/secrets_mgmt/consumers.py.

Two mechanisms, tested two ways. The `consumer_tags` cases below pin the name-routing table
against hand-written expectations. The guards after them derive their expectations from the
tree instead — the committed registry and the deploy tag list —
because name routing has already been wrong in production twice and a hand-list would have
agreed with it both times.

Run: uv run pytest scripts/secrets_mgmt/tests/test_secret_consumers.py
"""

from pathlib import Path

from secrets_mgmt.consumers import (
    FLUX_PATH,
    consumer_commands,
    consumer_tags,
)
from secrets_mgmt.rotation_tools import load_registry


# ── consumer mapping (which redeploys apply a rotated token) ─────────────────
def test_consumer_tags_of_a_token_no_arm_maps_are_empty():
    # `kopia_restore_drill_push_token` is absent from both secrets.yml and secret_rotation.yml,
    # so no arm maps it and it resolves to no consumer.
    assert consumer_tags("kopia_restore_drill_push_token") == ()


def test_consumer_tags_routes_the_bridge_prefix_to_the_bridge():
    """`monitor_bridge_ha_token` carries the prefix for Kuma history but is an HA API token."""
    assert consumer_tags("monitor_bridge_ha_token") == ("monitor-bridge",)


# DECIDED: no test here asserts a cross-host token is not badly overdue. One did, and on
# 2026-10-25 the calendar turned master and two unrelated PRs red in the same minute with no
# code change. Overdue is runtime state: SecretRotationAuditFailed pages the daily audit for any
# overdue token, cross-host ones included, and `cmd_audit --check` deliberately ignores it.
# `test_no_test_file_reads_the_live_clock` refuses the bare `today()` call it was built on.


# ── consumer_tag correctness ────────────────────────────────────────────────
# Nothing else proves a token's consumer tag is right, which is
# this estate's recurring guard-scope shape: a check written alongside a fix inherits the
# fix's scope. Routing every `monitor_bridge_*` name to monitor-bridge by prefix is wrong
# when such a token is pushed from another role's health script entirely —
# the prefix is a Kuma monitor-history artefact, kept so renaming the monitor would not lose
# its history. For those, `rotate --commit` would write a new value, deploy a role that renders
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
    reads as deployed. A token pushed by a SETUP role with no `containers_list` entry resolves no
    tag instead of naming its role for this reason.
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


# ── the Flux path for a k8s consumer (D3.4) ──
# d3-secrets.md, *Rotating a value read by both planes*: Flux substitutes the value from the
# cluster file, so each k8s consumer's line names the operator's `write` and the landing. No line
# names `deploy.sh`, which exits 5 on every k8s tag. The fixture is a whole repo because
# `consumer_commands` takes `repo`.
_FIXTURE_SECRET = "fixture_issue_4983_api_key"


def _repo_with_two_k8s_consumers(tmp_path: Path) -> str:
    """A repo whose secret is rendered by the k8s roles `bento-pdf` and `sonarr`."""
    for role in ("bento-pdf", "sonarr"):
        template = tmp_path / "ansible/roles/k8s" / role / "templates/secret.yaml.j2"
        template.parent.mkdir(parents=True)
        template.write_text(f"apiKey: {{{{ {_FIXTURE_SECRET} }}}}\n")
    return str(tmp_path)


def test_consumer_commands_prints_the_flux_path_for_every_k8s_consumer(tmp_path):
    commands = consumer_commands(
        _FIXTURE_SECRET, repo=_repo_with_two_k8s_consumers(tmp_path)
    )

    assert commands == [FLUX_PATH % "bento-pdf", FLUX_PATH % "sonarr"]
    assert "`uv run python scripts/secrets_mgmt/cluster_vars.py write`" in commands[0]
