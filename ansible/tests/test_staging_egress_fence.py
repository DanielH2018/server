"""The staging guest must be fenced off the production LAN, by a filter of the right shape.

The libvirt staging network is `<forward mode='nat'/>` with no destination constraint, so
without an explicit rule the guest reaches the whole production LAN — and reaches it
masqueraded as daniel-server, a trusted node. That defeats every source-IP control production
has: authelia's `policy: bypass` rules are scoped to `lan_subnet`, and daniel-pi's wg-easy
admin UI is unauthenticated on a LAN-only premise. Measured live on 2026-08-27 before any
fence existed: MetalLB VIP 301, k3s API 401, wg-easy 200.

The fence is a libvirt nwfilter attached to the guest's interface. It is deliberately NOT a
ufw `route deny`: that was the first attempt, it deployed cleanly, `ufw status` listed it, and
it was inert, because libvirt's own FORWARD accept is reached first. The whole history is at
roles/setup/initial_setup/tasks/network.yml, where the rule used to live.

These tests check the filter's SHAPE and its ATTACHMENT, which is all a repo-side check can
see. Whether it fires is a property of the running host —
`scripts/diagnostics/staging_egress_probe.py`, run on daniel-server, is the gate for that half.
Neither check subsumes the other: a correct filter can be unattached, and an attached filter
can be pointed at the wrong network. The inert ufw rule passed a whole file of shape tests.

The pair that matters most is the two CIDR tests at the end. A fence keyed to a network that
does not contain the guest is not a weaker fence, it is no fence at all, and it reads green
in every listing the host offers.
"""

from __future__ import annotations

import ipaddress
import re
import xml.etree.ElementTree as ET

import yaml

from _helpers import ALL_VARS, ROLES, jinja_env

HYPERVISOR = ROLES / "setup" / "hypervisor"
NWFILTER_TEMPLATE = HYPERVISOR / "templates" / "staging-nwfilter.xml.j2"
DOMAIN_TEMPLATE = HYPERVISOR / "templates" / "staging-vm.xml.j2"
HYPERVISOR_DEFAULTS = HYPERVISOR / "defaults" / "main.yml"
NETWORK_TASKS = HYPERVISOR / "tasks" / "network.yml"
GUEST_TASKS = HYPERVISOR / "tasks" / "guest.yml"
FIREWALL_TASKS = ROLES / "setup" / "initial_setup" / "tasks" / "network.yml"

CIDR_VAR = "staging_net_cidr"
LAN_VAR = "lan_subnet"
FILTER_NAME_VAR = "hypervisor_staging_nwfilter_name"


def _all_vars():
    return yaml.safe_load(ALL_VARS.read_text())


def _hypervisor_defaults():
    return yaml.safe_load(HYPERVISOR_DEFAULTS.read_text())


def _filter_name():
    return _hypervisor_defaults()[FILTER_NAME_VAR]


def _rendered_filter():
    """Render the nwfilter template and parse it as the XML libvirt will be handed."""
    context = {**_all_vars(), **_hypervisor_defaults()}
    # The shared env, not a bare jinja2 one: the template calls `to_uuid`, an Ansible filter.
    rendered = jinja_env().from_string(NWFILTER_TEMPLATE.read_text()).render(context)
    try:
        return ET.fromstring(rendered)
    except ET.ParseError as exc:  # pragma: no cover - only on a broken template
        raise AssertionError(
            f"{NWFILTER_TEMPLATE} did not render to parseable XML ({exc}). libvirt would "
            f"reject the define, and the guest would run unfenced."
        ) from exc


def _rules():
    rules = _rendered_filter().findall("rule")
    assert rules, (
        f"{NWFILTER_TEMPLATE} rendered a filter with no <rule> at all. libvirt accepts an "
        f"empty filter and attaches it happily, so this is the shape that reads green from "
        f"the host while fencing nothing."
    )
    return rules


def test_the_filter_pins_its_uuid():
    """Without this the role deploys once and fails on every re-run.

    `virsh nwfilter-define` is not `net-define`. Handed XML with no <uuid> it mints a fresh
    one and then refuses the name collision — "filter 'x' already exists with uuid ...".
    Measured on daniel-server 2026-08-28; it is what broke the first deploy of this role.
    """
    uuid = _rendered_filter().findtext("uuid")
    assert uuid and uuid.strip(), (
        f"{NWFILTER_TEMPLATE} renders no <uuid>. libvirt generates one on the first define "
        f"and then rejects every define after it, so the role would be green once and red "
        f"forever after — including on any host that already carries the filter."
    )


def test_the_fence_drops_rather_than_accepts():
    actions = {r.get("action") for r in _rules()}
    assert actions == {"drop"}, (
        f"{NWFILTER_TEMPLATE} declares rule actions {sorted(actions)}. Every rule here must "
        f"be 'drop': an 'accept' rule in a root-chain filter short-circuits the traffic it "
        f"matches, which is the opposite of what this filter exists to do."
    )


def test_the_fence_filters_traffic_leaving_the_guest():
    directions = {r.get("direction") for r in _rules()}
    assert directions == {"out"}, (
        f"{NWFILTER_TEMPLATE} declares rule directions {sorted(directions)}. libvirt reads "
        f"'out' as leaving the guest, which is the hazard; 'in' would block production "
        f"reaching staging, which nothing needs, and leave the real hole open."
    )


def test_the_fence_targets_the_production_lan():
    lan = ipaddress.ip_network(_all_vars()[LAN_VAR])
    targeted = set()
    for rule in _rules():
        for ip in rule.findall("ip"):
            addr, mask = ip.get("dstipaddr"), ip.get("dstipmask")
            assert addr and mask, (
                f"a rule in {NWFILTER_TEMPLATE} has dstipaddr={addr!r} dstipmask={mask!r}. "
                f"libvirt treats a missing destination as 'any', so a rule that lost its "
                f"destination silently blackholes the guest's internet egress instead — "
                f"which the probe reports as a broken fence, correctly."
            )
            targeted.add(ipaddress.ip_network(f"{addr}/{mask}"))
    assert targeted == {lan}, (
        f"{NWFILTER_TEMPLATE} fences {sorted(str(n) for n in targeted)}, not the production "
        f"LAN {lan} from {LAN_VAR}. `dest` must be the SAME variable authelia's bypass rules "
        f"use — the fence and the control it protects have to agree by construction."
    )


def test_the_fence_does_not_block_the_staging_network_itself():
    """Fencing the guest's own subnet would cut it off from its gateway and from Ansible."""
    staging = ipaddress.ip_network(_all_vars()[CIDR_VAR])
    for rule in _rules():
        for ip in rule.findall("ip"):
            blocked = ipaddress.ip_network(
                f"{ip.get('dstipaddr')}/{ip.get('dstipmask')}"
            )
            assert not blocked.overlaps(staging), (
                f"{NWFILTER_TEMPLATE} drops traffic to {blocked}, which overlaps the staging "
                f"network {staging}. The guest reaches daniel-server on that network, so this "
                f"would break ssh, Ansible and the acceptance probe itself."
            )


def test_the_guest_interface_references_the_fence():
    """A defined filter nothing references is the exact shape of an inert fence."""
    domain = DOMAIN_TEMPLATE.read_text()
    interfaces = re.findall(r"<interface\b.*?</interface>", domain, re.S)
    assert interfaces, f"no <interface> found in {DOMAIN_TEMPLATE}."
    for iface in interfaces:
        assert FILTER_NAME_VAR in iface, (
            f"an <interface> in {DOMAIN_TEMPLATE} carries no <filterref> naming "
            f"{{{{ {FILTER_NAME_VAR} }}}}. libvirt applies a filter only to interfaces that "
            f"reference it, so the guest would boot unfenced with the filter defined."
        )


def test_the_filter_is_defined_before_the_guest_that_references_it():
    """libvirt refuses to start a domain whose <filterref> names a filter it does not know."""
    defines = [
        t
        for t in yaml.safe_load(NETWORK_TASKS.read_text())
        if "nwfilter-define" in str(t.get("ansible.builtin.command", ""))
    ]
    assert defines, (
        f"no `virsh nwfilter-define` in {NETWORK_TASKS}. network.yml runs before guest.yml "
        f"(roles/setup/hypervisor/tasks/install.yml), which is the ordering the domain's "
        f"<filterref> depends on; moving the define into guest.yml breaks a cold build."
    )


def test_a_running_guest_with_an_unfenced_interface_is_restarted():
    """`virsh define` writes config only, so a running guest keeps its unfenced interface."""
    corrections = [
        t
        for t in yaml.safe_load(GUEST_TASKS.read_text())
        if "destroy" in str(t.get("ansible.builtin.command", ""))
    ]
    assert len(corrections) == 1, (
        f"expected exactly one `virsh destroy` in {GUEST_TASKS}, found {len(corrections)}. "
        f"Without it, adding the fence to the domain template updates the persistent config "
        f"and the live guest keeps running unfenced — a deploy that reports changed and "
        f"changes nothing the probe can see."
    )
    guard = str(corrections[0].get("when", ""))
    assert "filterref" in guard, (
        f"the destroy in {GUEST_TASKS} is guarded by {guard!r}, which does not test the live "
        f"interface for a filterref. Ungated, this restarts the guest on every run."
    )


def test_no_ufw_rule_claims_to_fence_staging():
    """The inert first attempt must be deleted, not left listed as protection."""
    for task in yaml.safe_load(FIREWALL_TASKS.read_text()):
        rule = task.get("community.general.ufw")
        if not isinstance(rule, dict) or not rule.get("route"):
            continue
        assert rule.get("delete"), (
            f"{FIREWALL_TASKS} declares a ufw `route` rule ({task.get('name')!r}) that is not "
            f"a delete. A routed rule on this host cannot fence the staging guest — libvirt's "
            f"FORWARD accept is reached first, which is why DEFAULT_FORWARD_POLICY=DROP never "
            f"blocked it either. Such a rule reads as protection and provides none."
        )


def test_the_staging_cidr_contains_the_guest():
    """The failure this catches reads green everywhere: a fence around an empty network."""
    all_vars = _all_vars()
    net = ipaddress.ip_network(all_vars[CIDR_VAR])
    guest = ipaddress.ip_address(all_vars["staging_vm_ip"])
    assert guest in net, (
        f"{CIDR_VAR} is {net}, which does not contain staging_vm_ip {guest}. Every check here "
        f"would pass and the guest would sit outside the network they describe."
    )


def test_the_staging_cidr_agrees_with_the_network_the_hypervisor_builds():
    """Two roles describe one network in two forms; drift between them disarms the fence."""
    all_vars = _all_vars()
    hv = _hypervisor_defaults()
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
