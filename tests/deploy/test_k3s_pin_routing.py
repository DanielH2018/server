"""A bump of the k3s pin routes to the Flux-owned `system-upgrade` tag, not to a setup role (H6.1b).

A diff that changes only `k3s_version` in the `system-upgrade` role's defaults derives the tag
`system-upgrade`, which `land.sh` waits on through promotion.

Run: uv run pytest tests/deploy/test_k3s_pin_routing.py
"""

from deploy_tools.land_lib import land_tags

NEW = "k8s/roles/system-upgrade/defaults/main.yml"


def test_a_pin_bump_derives_the_flux_tag():
    assert land_tags.derive([NEW], 1).tags == ["system-upgrade"]
