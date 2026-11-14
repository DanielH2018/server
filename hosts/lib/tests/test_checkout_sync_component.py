"""The rendered `checkout-sync` component in daniel-box's package (H7.51).

These tests hold what the plan fixes about the unit: the 10-minute monotonic timer, the run inside
the git-tree lock, the builder's token as a credential, the shared CI contexts and walk bound,
and the modules the script imports. The census row `component-timers-carry-a-contract` holds the
contract, and test_checkout_sync.py covers the script's behaviour.

Run: uv run pytest hosts/lib/tests/test_checkout_sync_component.py
"""

import pytest
import yaml
from _checkout_package import render_daniel_box

from lib.estate import REPO_INVENTORY
from lib.render_context import render_context
from lib.repo_paths import REPO
from renderer.renderer_lib import host as host_mode

UNITS = "/usr/lib/systemd/system"
LIB = "/usr/lib/homelab/checkout-sync"


@pytest.fixture(scope="module")
def package(tmp_path_factory) -> host_mode.Package:
    return render_daniel_box(
        tmp_path_factory.mktemp("checkout-sync"), "failure-alert", "checkout-sync"
    )


def _text(package: host_mode.Package, dst: str) -> str:
    return package.files[dst][0].decode()


def _box() -> dict:
    return render_context(None, "daniel-box", inventory=REPO_INVENTORY)


def test_the_timer_fires_every_10_minutes(package):
    timer = _text(package, f"{UNITS}/homelab-checkout-sync.timer")
    assert "OnBootSec=10min" in timer
    assert "OnUnitActiveSec=10min" in timer
    assert "homelab-checkout-sync.timer" in package.enable_links()


def test_the_service_runs_in_the_tree_lock_with_the_shared_ci_gate(package):
    box = _box()
    service = _text(package, f"{UNITS}/homelab-checkout-sync.service")
    assert f"User={box['sys_user']}\n" in service
    assert f"WorkingDirectory=/home/{box['sys_user']}/server\n" in service
    assert (
        f"ExecStart=/usr/bin/flock -n -E 75 {box['server_git_tree_lock']} " in service
    )
    assert f"{LIB}/checkout_sync.py\n" in service
    assert "SuccessExitStatus=75\n" in service
    assert "OnFailure=homelab-alert@%n.service\n" in service
    assert (
        "LoadCredential=github_token:/run/credstore/homelab_builder_github_token\n"
        in service
    )
    contexts = ",".join(box["ci_required_contexts"])
    assert f'Environment="CI_CONTEXTS={contexts}"\n' in service
    assert (
        f"Environment=CI_ANCESTOR_WALK_MAX={box['ci_ancestor_walk_max']}\n" in service
    )
    assert "homelab_builder_github_token" in package.credentials


def test_the_script_ships_beside_the_modules_it_imports(package):
    for name in (
        "checkout_sync.py",
        "ci_verdict.py",
        "builder_ci_grade.py",
        "host_lib.py",
    ):
        assert f"{LIB}/{name}" in package.files, name


# The deployer's units every host's package retires (H7.52). checkout-sync replaces the timer's
# fast-forward, so a host still running it would fast-forward the checkout from a stale copy.
DEPLOYER_UNITS = (
    {"unit": "gitops-deploy.timer"},
    {"unit": "gitops-deploy.service"},
    {"unit": "gitops-deploy-alert.service"},
)
HOSTS = ("daniel-box", "daniel-server", "daniel-pi")


def deployer_units_not_retired(legacy: list[dict]) -> list[dict]:
    """The deployer units a host package's `legacy:` list does not retire."""
    return [unit for unit in DEPLOYER_UNITS if unit not in legacy]


@pytest.mark.parametrize("host", HOSTS)
def test_every_host_package_retires_the_deployers_units_is_clean(host):
    package = yaml.safe_load((REPO / "hosts" / host / "package.yml").read_text())
    assert deployer_units_not_retired(package["legacy"]) == []


def test_a_package_that_keeps_the_deployers_timer_is_flagged():
    legacy = [unit for unit in DEPLOYER_UNITS if unit["unit"] != "gitops-deploy.timer"]
    assert deployer_units_not_retired(legacy) == [{"unit": "gitops-deploy.timer"}]
