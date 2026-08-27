"""The staging guest must be fenced off the production LAN, by a rule of the right shape.

The libvirt staging network is `<forward mode='nat'/>` with no destination constraint, so
without an explicit rule the guest reaches the whole production LAN — and reaches it
masqueraded as daniel-server, a trusted node. That defeats every source-IP control production
has: authelia's `policy: bypass` rules are scoped to `lan_subnet`, and daniel-pi's wg-easy
admin UI is unauthenticated on a LAN-only premise. Measured live on 2026-08-27 before the
fence existed: MetalLB VIP 301, k3s API 401, wg-easy 200.

These tests check the rule's SHAPE, which is all a repo-side check can see. Whether the rule
actually fires depends on its position relative to libvirt's own accept for the bridge, and
that is invisible here — `scripts/diagnostics/staging_egress_probe.py`, run from inside the
guest, is the gate for that half. Neither check subsumes the other: a correct rule can be
inert, and a firing rule can be pointed at the wrong network.

The pair that matters most is the last two. A fence keyed to a CIDR that does not contain the
guest is not a weaker fence, it is no fence at all, and it reads green in `ufw status`.
"""

import ipaddress

import yaml

from _helpers import ALL_VARS, ROLES

NETWORK_TASKS = ROLES / "setup" / "initial_setup" / "tasks" / "network.yml"
HYPERVISOR_DEFAULTS = ROLES / "setup" / "hypervisor" / "defaults" / "main.yml"

CIDR_VAR = "staging_net_cidr"
LAN_VAR = "lan_subnet"


def _tasks():
    tasks = yaml.safe_load(NETWORK_TASKS.read_text())
    assert tasks, (
        f"{NETWORK_TASKS} parsed to no tasks — check the loader, not the playbook."
    )
    return tasks


def _fence():
    fences = [
        t
        for t in _tasks()
        if isinstance(t.get("community.general.ufw"), dict)
        and t["community.general.ufw"].get("route")
    ]
    assert len(fences) == 1, (
        f"expected exactly one ufw `route` rule in {NETWORK_TASKS}, found {len(fences)}. The "
        f"staging egress fence is the only routed rule this repo declares; a second one needs "
        f"its own reasoning written at the line before this test is relaxed."
    )
    return fences[0]


def test_the_fence_denies_rather_than_allows():
    rule = _fence()["community.general.ufw"]
    assert rule.get("rule") == "deny", (
        f"the routed rule in {NETWORK_TASKS} is {rule.get('rule')!r}, not 'deny'. A routed "
        f"allow between these two networks is the hole this rule exists to close."
    )


def test_the_fence_runs_only_where_a_hypervisor_does():
    """Every host imports this role; only daniel-server has a guest to fence."""
    guard = _fence().get("when", "")
    assert "has_hypervisor" in str(guard), (
        f"the fence in {NETWORK_TASKS} is guarded by {guard!r}. It must be `has_hypervisor`, "
        f"the flag that means this host runs the staging guest — gating on a hostname would "
        f"silently stop fencing if the guest moved."
    )


def test_the_fence_names_variables_and_not_literal_networks():
    rule = _fence()["community.general.ufw"]
    for field, want in (("src", CIDR_VAR), ("dest", LAN_VAR)):
        assert want in str(rule.get(field, "")), (
            f"the fence's {field} in {NETWORK_TASKS} is {rule.get(field)!r}, which does not "
            f"reference {{{{ {want} }}}}. A literal here drifts from the network it is meant "
            f"to fence, and `dest` in particular must be the SAME variable authelia's bypass "
            f"rules use — the fence and the control it protects have to agree by construction."
        )


def test_the_staging_cidr_contains_the_guest():
    """The failure this catches reads green everywhere: a rule fencing an empty network."""
    all_vars = yaml.safe_load(ALL_VARS.read_text())
    net = ipaddress.ip_network(all_vars[CIDR_VAR])
    guest = ipaddress.ip_address(all_vars["staging_vm_ip"])
    assert guest in net, (
        f"{CIDR_VAR} is {net}, which does not contain staging_vm_ip {guest}. The fence would "
        f"be installed, list cleanly in `ufw status`, and block nothing at all."
    )


def test_the_staging_cidr_agrees_with_the_network_the_hypervisor_builds():
    """Two roles describe one network in two forms; drift between them disarms the fence."""
    all_vars = yaml.safe_load(ALL_VARS.read_text())
    hv = yaml.safe_load(HYPERVISOR_DEFAULTS.read_text())
    net = ipaddress.ip_network(all_vars[CIDR_VAR])
    gateway = ipaddress.ip_address(hv["hypervisor_staging_net_gateway"])
    assert gateway in net, (
        f"{CIDR_VAR} ({net}) does not contain the libvirt network's gateway {gateway} from "
        f"{HYPERVISOR_DEFAULTS}. One of the two was changed without the other."
    )
    netmask = ipaddress.ip_address(hv["hypervisor_staging_net_netmask"])
    assert str(net.netmask) == str(netmask), (
        f"{CIDR_VAR} has netmask {net.netmask} but {HYPERVISOR_DEFAULTS} builds the network "
        f"with {netmask}. A wider CIDR here fences addresses libvirt never hands out; a "
        f"narrower one leaves part of the guest network unfenced."
    )


def test_the_fence_does_not_target_the_production_lan_as_a_source():
    """Reversing src and dest would fence production out of staging and leave the hole open."""
    rule = _fence()["community.general.ufw"]
    assert LAN_VAR not in str(rule.get("src", "")), (
        f"the fence's src in {NETWORK_TASKS} references {LAN_VAR}. src and dest are reversed: "
        f"this blocks production reaching staging, which nothing needs, and leaves staging "
        f"reaching production, which is the actual hazard."
    )
