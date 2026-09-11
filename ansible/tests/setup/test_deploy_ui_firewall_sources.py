"""deploy-ui's port is reachable only from the cluster: both node IPs (flannel masquerades
pod→LAN traffic to the sending node) and the pod CIDR. Dropping one silently breaks Traefik
on that node; widening to lan_subnet removes Authelia from in front of a deploy button."""

from _helpers import REPO
from lib import yaml_fast

DEFAULTS = REPO / "ansible/roles/setup/deploy_ui/defaults/main.yml"


def sources():
    return yaml_fast.safe_load(DEFAULTS.read_text())["deploy_ui_allowed_sources"]


def test_sources_name_the_pod_cidr_and_both_nodes_is_clean():
    got = sources()
    assert "{{ k3s_pod_cidr }}" in got
    assert "{{ server_ip }}" in got
    assert any("k3s_agent_node_ips" in s for s in got)


def test_sources_never_widen_to_the_lan_is_flagged():
    assert not any("lan_subnet" in s for s in sources())
