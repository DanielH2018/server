"""A bump of the k3s pin routes to the Flux-owned `system-upgrade` tag, not to a setup role (H6.1b).

The pin moved from `ansible/roles/setup/k3s/defaults/main.yml`, whose change the deployer
records as a `manual_plane` line for `k3s` and nothing applies, to the `system-upgrade` role's
defaults. A diff that changes only `k3s_version` there now derives the tag `system-upgrade`,
which `land.sh` waits on through promotion, and no setup role. The same diff on the old file
still derives setup role `k3s`, which the second test holds so the routing difference is what
the move changed rather than a test that reads nothing.

Run: uv run pytest ansible/tests/deploy/test_k3s_pin_routing.py
"""

from deploy_tools.land_lib import land_tags
from deployer_lib.deploy_changes import setup_roles_for
from lib import yaml_fast
from lib.repo_paths import K3S_DEFAULTS, REPO, SYSTEM_UPGRADE_DEFAULTS

PIN_KEYS = ("k3s_version", "k3s_install_script_url", "k3s_install_script_sha256")
NEW = "ansible/roles/k8s/system-upgrade/defaults/main.yml"
OLD = "ansible/roles/setup/k3s/defaults/main.yml"


def test_a_pin_bump_derives_the_flux_tag_and_no_setup_role():
    assert land_tags.derive([NEW], 1).tags == ["system-upgrade"]
    assert setup_roles_for(NEW) == set()


def test_the_same_diff_on_the_old_file_derives_setup_role_k3s():
    assert "k3s" in setup_roles_for(OLD)


def test_the_pin_and_its_two_siblings_live_in_the_system_upgrade_defaults_alone():
    new = yaml_fast.safe_load(SYSTEM_UPGRADE_DEFAULTS.read_text())
    old = yaml_fast.safe_load(K3S_DEFAULTS.read_text())
    assert REPO / NEW == SYSTEM_UPGRADE_DEFAULTS and REPO / OLD == K3S_DEFAULTS
    assert all(key in new for key in PIN_KEYS)
    assert not any(key in old for key in PIN_KEYS)
