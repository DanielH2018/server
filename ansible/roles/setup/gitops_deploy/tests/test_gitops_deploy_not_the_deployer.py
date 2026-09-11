"""A host whose inventory says `has_gitops: false` refuses to tick (#1733).

The role installs the deployer only `when: has_gitops`, and until 2026-09-09 that was the only
gate: daniel-server carried `has_gitops: false` and ticked a live timer for three weeks from a
payload the role had stopped updating. `declares_no_gitops` is the code's own reading of the
same host_vars; `refuse_unless_deployer` raises `NotTheDeployerHost` at the top of `main()`,
and `entrypoint()` turns it into a one-line exit 0 with no post and no last_run.

The clean cases are the half that matters: a false refusal on the real deployer parks every
landing in the fleet, so every ambiguous shape must proceed.
Run: uv run pytest ansible/roles/setup/gitops_deploy/tests/test_gitops_deploy_not_the_deployer.py
"""

# ansible/roles/setup/gitops_deploy/tests/test_gitops_deploy_not_the_deployer.py

import json
from pathlib import Path

import pytest

import deploy_alerts
from deploy_inventory import declares_no_gitops

ORIGIN = "2" * 40
HOST_VARS = Path(__file__).resolve().parents[4] / "inventory" / "host_vars"


# ── declares_no_gitops(): the paired proof ────────────────────────────────────────────────────
def test_a_top_level_has_gitops_false_is_flagged():
    assert declares_no_gitops(
        "containers_list: []\nhas_gitops: false\nhas_docker: false\n"
    )


def test_a_trailing_comment_is_still_flagged():
    assert declares_no_gitops("has_gitops: false  # reaped 2026-09-09\n")


@pytest.mark.parametrize("value", ["no", "off", "False", "NO"])
def test_every_yaml_false_literal_is_flagged(value):
    assert declares_no_gitops(f"has_gitops: {value}\n")


def test_the_live_inventory_is_read_the_way_the_role_reads_it():
    """Named members, not invented strings: the regex must fire on daniel-server's file as it
    sits on disk and stay quiet on daniel-box's, or it guards a spelling nobody uses."""
    assert declares_no_gitops((HOST_VARS / "daniel-server.yml").read_text())
    assert declares_no_gitops((HOST_VARS / "daniel-pi.yml").read_text())
    assert not declares_no_gitops((HOST_VARS / "daniel-box.yml").read_text())


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(None, id="host_vars file missing or unreadable"),
        pytest.param("", id="empty file"),
        pytest.param(
            "containers_list: []\n", id="key absent (group_vars defaults it true)"
        ),
        pytest.param("has_gitops: true\n", id="true"),
        pytest.param("  has_gitops: false\n", id="indented, not top-level"),
        pytest.param("# has_gitops: false\n", id="commented out"),
        pytest.param("has_gitops: falsey\n", id="not the literal false"),
        pytest.param("has_gitops_extra: false\n", id="a longer key"),
    ],
)
def test_every_ambiguous_shape_is_clean(text):
    assert not declares_no_gitops(text)


# ── main(): the refusal lands before any state is touched ─────────────────────────────────────
def test_main_refuses_on_a_host_that_declares_no_gitops(gitops_deploy, tick, state_dir):
    deploy_alerts.write_pending(
        gitops_deploy.PENDING_ALERTS_FILE, {"secrets:" + ORIGIN: "queued last tick"}
    )
    tick.declare("containers_list: []\nhas_gitops: false\n")
    with pytest.raises(gitops_deploy.NotTheDeployerHost, match="has_gitops: false"):
        gitops_deploy.main(tick.tools)
    assert tick.posts == [], "the refusal must not drain the alert queue"
    assert json.loads((state_dir / "pending_alerts.json").read_text()) == {
        "secrets:" + ORIGIN: "queued last tick"
    }
    assert tick.merges == [] and tick.playbooks == []


def test_main_proceeds_when_the_host_declares_has_gitops_true(gitops_deploy, tick):
    tick.declare("containers_list: []\nhas_gitops: true\n")
    tick.origin = tick.local
    assert gitops_deploy.main(tick.tools) == 0


def test_main_proceeds_when_the_host_has_no_host_vars_file(gitops_deploy, tick):
    # The `tick` fixture writes none unless a test calls declare(); this is the fail-open case.
    tick.origin = tick.local
    assert gitops_deploy.main(tick.tools) == 0


# ── entrypoint(): a refusal is one line, exit 0, and writes nothing ───────────────────────────
def test_entrypoint_turns_the_refusal_into_a_silent_exit_0(
    gitops_deploy, tick, state_dir, capsys
):
    tick.declare("containers_list: []\nhas_gitops: false\n")
    assert gitops_deploy.entrypoint(tick.tools) == 0
    assert tick.posts == [], (
        "a refusal must not page from a webhook this host should not hold"
    )
    assert not (state_dir / "behind_since").exists(), (
        "a refusal must not record the behind-origin marker"
    )
    assert not (state_dir / "last_run").exists(), (
        "a refusal must not stamp liveness onto state nothing on this host reads"
    )
    out = capsys.readouterr().out
    assert "gitops-deploy: test-host declares has_gitops: false" in out
    assert "Traceback" not in out
