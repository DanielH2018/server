"""Every guest on the staging network must be fenced off the production LAN, by a filter of
the right shape.

The only guest wearing the fence is the etcd restore drill's throwaway one, which holds the
cluster token and the R2 write credentials for the whole backup bucket.

The libvirt staging network is `<forward mode='nat'/>` with no destination constraint, so
without an explicit rule the guest reaches the whole production LAN — and reaches it
masqueraded as daniel-server, a trusted node. That defeats every source-IP control production
has: authelia's `policy: bypass` rules are scoped to `lan_subnet`, and daniel-pi's wg-easy
admin UI is unauthenticated on a LAN-only premise. Measured without a
fence: MetalLB VIP 301, k3s API 401, wg-easy 200.

The fence is a libvirt nwfilter attached to the guest's interface. It is deliberately NOT a
ufw `route deny`: that deploys cleanly and `ufw status` lists it, but it is inert, because
libvirt's own FORWARD accept is reached first.

Two halves live here. The first checks the filter's SHAPE and its ATTACHMENT, which is all a
check that never leaves the repo can see: a correct filter that is unattached reads green in
every listing the host offers, and the inert ufw rule passed a whole file of shape tests.

The second half is the reachability gate, and two tests of it live here: that the leg dials
every range this filter drops, and that it runs before the guest is handed any credential.
Neither can be seen from inside the guest. Its verdicts are
test_staging_egress_fence_fires.py, driven through the _fence_probe harness.

The pair that matters most is the two CIDR tests at the end. A fence keyed to a network that
does not contain the guest is not a weaker fence, it is no fence at all, and it reads green
in every listing the host offers.
"""

import ipaddress
import xml.etree.ElementTree as ET

from lib import yaml_fast

from _fence_probe import (
    CONTROL,
    ORCHESTRATOR,
    fence_targets_block,
    dialled_addresses,
    rendered_orchestrator,
)
from _helpers import ALL_VARS, HOST_VARS, ROLES
from _setup_render import rendered_setup_text

HYPERVISOR = ROLES / "setup" / "hypervisor"
K3S_DEFAULTS = ROLES / "setup" / "k3s" / "defaults" / "main.yml"
NWFILTER_TEMPLATE = HYPERVISOR / "templates" / "staging-nwfilter.xml.j2"
NETWORK_TEMPLATE = HYPERVISOR / "templates" / "staging-network.xml.j2"
DOMAIN_TEMPLATE = HYPERVISOR / "templates" / "etcd-drill-vm.xml.j2"
HYPERVISOR_DEFAULTS = HYPERVISOR / "defaults" / "main.yml"
NETWORK_TASKS = HYPERVISOR / "tasks" / "network.yml"
FIREWALL_TASKS = ROLES / "setup" / "initial_setup" / "tasks" / "network.yml"

CIDR_VAR = "staging_net_cidr"
LAN_VAR = "lan_subnet"
POD_CIDR_VAR = "k3s_pod_cidr"
SERVICE_CIDR_VAR = "k3s_service_cidr"
FILTER_NAME_VAR = "hypervisor_staging_nwfilter_name"


def _load_host_vars(host: str):
    return yaml_fast.safe_load((HOST_VARS / f"{host}.yml").read_text()) or {}


def _all_vars():
    return yaml_fast.safe_load(ALL_VARS.read_text())


def _hypervisor_defaults():
    return yaml_fast.safe_load(HYPERVISOR_DEFAULTS.read_text())


def _filter_name():
    return _hypervisor_defaults()[FILTER_NAME_VAR]


def _rendered_filter():
    """The nwfilter as the setup plane renders it, parsed as the XML libvirt will be handed.

    `_setup_render` rather than a hand-built context: it is the same layering
    `validate/setup_templates.py` renders this role with, so a value that moves between
    `group_vars/all.yml` and the role's defaults cannot change what this guard sees.
    """
    rendered = rendered_setup_text("hypervisor", "staging-nwfilter.xml.j2")
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


def _fenced_networks(rules=None):
    """Every destination the filter drops, as networks. Defaults to the rendered template."""
    targeted = set()
    for rule in _rules() if rules is None else rules:
        for ip in rule.findall("ip"):
            addr, mask = ip.get("dstipaddr"), ip.get("dstipmask")
            assert addr and mask, (
                f"a rule in {NWFILTER_TEMPLATE} has dstipaddr={addr!r} dstipmask={mask!r}. "
                f"libvirt treats a missing destination as 'any', so a rule that lost its "
                f"destination silently blackholes the guest's internet egress instead — "
                f"which the probe reports as a broken fence, correctly."
            )
            targeted.add(ipaddress.ip_network(f"{addr}/{mask}"))
    return targeted


def _expected_networks():
    all_vars = _all_vars()
    return {
        ipaddress.ip_network(all_vars[v])
        for v in (LAN_VAR, POD_CIDR_VAR, SERVICE_CIDR_VAR)
    }


def fence_disagreement(targeted, expected):
    """The verdict, as (unfenced, over-fenced). Both halves are defects, in both directions.

    Kept as a function rather than an inline `==` so the rejecting half below can drive the
    same comparison the real test drives, instead of asserting set arithmetic of its own.
    """
    return (
        sorted(str(n) for n in expected - targeted),
        sorted(str(n) for n in targeted - expected),
    )


def test_the_fence_targets_every_production_range():
    """Set EQUALITY, deliberately, and widened from the variables rather than relaxed.

    Too narrow is a live defect: a fence on lan_subnet alone lets the guest read prod's
    unauthenticated Longhorn API on a ClusterIP the LAN rule cannot cover.
    Too broad is the other failure and is why this stays an equality — a rule that grew to
    cover 0.0.0.0/0 or 10.0.0.0/8 would swallow the guest's default route, and the probe
    would report that as a broken fence only because its internet control leg goes red.
    """
    unfenced, over_fenced = fence_disagreement(_fenced_networks(), _expected_networks())
    assert not unfenced and not over_fenced, (
        f"{NWFILTER_TEMPLATE} leaves {unfenced} unfenced and additionally fences "
        f"{over_fenced}, against {LAN_VAR}/{POD_CIDR_VAR}/{SERVICE_CIDR_VAR}. Each `dest` "
        f"must be the SAME variable the control it protects uses — the LAN one is authelia's "
        f"bypass scope, and the two cluster ones are what make a ClusterIP or a pod IP "
        f"unreachable from the guest."
    )


def test_the_range_check_rejects_the_shape_that_was_live():
    """The rejecting half, driving the real verdict function on a filter that fences lan_subnet only.

    A check that could not tell it apart from the correct filter would let the guest read
    prod's Longhorn API.
    """
    lan = _all_vars()[LAN_VAR]
    was_live = ET.fromstring(
        f"<filter name='x' chain='root'><rule action='drop' direction='out' priority='100'>"
        f"<ip dstipaddr='{lan.split('/')[0]}' dstipmask='{lan.split('/')[1]}'/>"
        f"</rule></filter>"
    ).findall("rule")
    unfenced, over_fenced = fence_disagreement(
        _fenced_networks(was_live), _expected_networks()
    )
    assert sorted(unfenced) == sorted(
        str(ipaddress.ip_network(_all_vars()[v]))
        for v in (POD_CIDR_VAR, SERVICE_CIDR_VAR)
    ), (
        f"the pre-fix filter reported {unfenced} unfenced. It must report exactly the pod "
        f"and Service CIDRs — anything else means the verdict function stopped seeing the "
        f"defect it was written for."
    )
    assert not over_fenced


def test_the_cluster_dns_ip_falls_inside_the_fenced_service_cidr():
    """Pins the premise that the Service CIDR is really the cluster's, not a guessed range.

    k3s_service_cidr is declared in group_vars while the address k3s actually hands CoreDNS
    lives in the k3s role's defaults. If the two drift, the fence names a range no Service is
    in — which fences nothing and reads green in every check above.
    """
    service_cidr = ipaddress.ip_network(_all_vars()[SERVICE_CIDR_VAR])
    k3s_defaults = yaml_fast.safe_load(K3S_DEFAULTS.read_text())
    dns_ip = ipaddress.ip_address(k3s_defaults["k3s_cluster_dns_ip"])
    assert dns_ip in service_cidr, (
        f"{SERVICE_CIDR_VAR} is {service_cidr}, which does not contain k3s_cluster_dns_ip "
        f"{dns_ip} from {K3S_DEFAULTS}. One of the two was changed without the other, and "
        f"the fence is around a range the cluster does not use."
    )


def test_fencing_the_clusters_own_ranges_still_assumes_one_node_on_this_bridge():
    """The trade-off the CIDR rules make, tied to the fact that would end it.

    The pod and Service CIDRs are k3s defaults, so a k3s guest on this bridge would carry the
    same two ranges. Dropping them is safe only because such a guest's own pod and Service
    traffic is delivered on its internal cni0 and by its own kube-proxy rules, never crossing
    the tap device this filter attaches to. A second node on this bridge would put pod-to-pod
    traffic on the wire, where the /16 drop would break it — so the fence would then need a
    source- or interface-scoped exception.

    The network declaring exactly one DHCP reservation is what says that has not happened.
    """
    network = ET.fromstring(rendered_setup_text("hypervisor", "staging-network.xml.j2"))
    reservations = network.findall("./ip/dhcp/host")
    assert len(reservations) == 1, (
        f"{NETWORK_TEMPLATE} declares {len(reservations)} DHCP reservations, so more than one "
        f"guest shares this bridge. Pod-to-pod traffic between them crosses the tap device the "
        f"fence attaches to, and the {POD_CIDR_VAR}/{SERVICE_CIDR_VAR} drop rules in "
        f"{NWFILTER_TEMPLATE} will break it. Scope those rules before adding the guest."
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
                f"would break the drill orchestrator's ssh."
            )


def test_the_guest_interface_references_the_fence():
    """A defined filter nothing references is the exact shape of an inert fence.

    On the RENDER, so the claim is that the guest's `<filterref>` names the filter libvirt was
    handed — the same string `_rendered_filter` reads. A guard on the template's text asserts
    only that the variable NAME appears, which stays true when the variable itself drifts.
    """
    domain = ET.fromstring(rendered_setup_text("hypervisor", "etcd-drill-vm.xml.j2"))
    interfaces = domain.findall("./devices/interface")
    assert interfaces, f"no <interface> found in {DOMAIN_TEMPLATE}."
    for iface in interfaces:
        filterref = iface.find("filterref")
        assert filterref is not None and filterref.get("filter") == _filter_name(), (
            f"an <interface> in {DOMAIN_TEMPLATE} renders no <filterref> naming "
            f"{_filter_name()} (from {FILTER_NAME_VAR}). libvirt applies a filter only to "
            f"interfaces that reference it, so the guest would boot unfenced with the filter "
            f"defined."
        )


def test_the_filter_is_defined_before_the_guest_that_references_it():
    """libvirt refuses to start a domain whose <filterref> names a filter it does not know."""
    defines = [
        t
        for t in yaml_fast.safe_load(NETWORK_TASKS.read_text())
        if "nwfilter-define" in str(t.get("ansible.builtin.command", ""))
    ]
    assert defines, (
        f"no `virsh nwfilter-define` in {NETWORK_TASKS}. network.yml runs before etcd_drill.yml "
        f"(roles/setup/hypervisor/tasks/install.yml), which is the ordering the domain's "
        f"<filterref> depends on; moving the define later breaks a cold build."
    )


def test_no_ufw_rule_claims_to_fence_staging():
    """The inert first attempt must be deleted, not left listed as protection."""
    for task in yaml_fast.safe_load(FIREWALL_TASKS.read_text()):
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
    guest = ipaddress.ip_address(_hypervisor_defaults()["hypervisor_etcd_drill_vm_ip"])
    assert guest in net, (
        f"{CIDR_VAR} is {net}, which does not contain the drill guest's address {guest}. Every "
        f"check here would pass and the guest would sit outside the network they describe."
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


def test_the_leg_dials_every_range_the_filter_fences():
    """Non-vacuity, tied to the filter rather than to a count.

    A leg that lost a target reads exactly like a fence that holds: every remaining dial still
    comes back refused. So each network the nwfilter drops must have a dial aimed inside it. The
    pod CIDR's is the one address that cannot be a literal — a pod IP is ephemeral, so the leg
    discovers it from the host's neighbour table, and the runs below exercise that path.
    """
    block = fence_targets_block()
    dialled = dialled_addresses(block)
    assert dialled, f"no dialled addresses parsed out of fence_targets(): {block}"
    assert "cni0" in block, (
        "the leg no longer discovers a pod IP from the host's neighbour table, so nothing dials "
        f"{POD_CIDR_VAR} at all"
    )
    pod_cidr = ipaddress.ip_network(_all_vars()[POD_CIDR_VAR])
    for network in _expected_networks() - {pod_cidr}:
        assert any(address in network for address in dialled), (
            f"no dial in fence_targets() falls inside the fenced network {network}. "
            f"{NWFILTER_TEMPLATE.name} drops that range and nothing measures it: every other "
            f"dial still comes back refused, so the leg reads green."
        )
    control = ipaddress.ip_address(CONTROL.split("//")[1])
    assert not any(control in network for network in _expected_networks()), (
        f"the control target {control} sits inside a fenced range, so it cannot prove the guest "
        f"kept its egress — a working fence would refuse it and the leg would call the fence "
        f"broken on every run."
    )


def test_the_leg_runs_before_the_guest_is_handed_any_credential():
    """Placement is the half of this that cannot be measured from inside the guest.

    A leg that ran after the staging tar would report the leak correctly and a minute too late:
    the cluster token and the R2 write credentials for the whole backup bucket would already be
    on a guest that can reach production.
    """
    body = rendered_orchestrator()
    main = body.index("main() {")
    leg = body.index("\n  fence_check\n", main)
    for marker, what in (
        ('. "$AGENT_ENV"', "the cluster token is read"),
        ("etcd-s3.env", "the R2 credentials are staged"),
        ("etcd-drill-guest-run --detached", "the drill is started"),
    ):
        assert leg < body.index(marker, main), (
            f"the fence leg runs after {what} in {ORCHESTRATOR.name}"
        )
