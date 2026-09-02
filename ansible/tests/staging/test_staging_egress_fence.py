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
from urllib.parse import urlparse

import staging_egress_probe
import yaml

from _helpers import ALL_VARS, HOST_VARS, ROLES, jinja_env

HYPERVISOR = ROLES / "setup" / "hypervisor"
K3S_DEFAULTS = ROLES / "setup" / "k3s" / "defaults" / "main.yml"
NWFILTER_TEMPLATE = HYPERVISOR / "templates" / "staging-nwfilter.xml.j2"
DOMAIN_TEMPLATE = HYPERVISOR / "templates" / "staging-vm.xml.j2"
HYPERVISOR_DEFAULTS = HYPERVISOR / "defaults" / "main.yml"
NETWORK_TASKS = HYPERVISOR / "tasks" / "network.yml"
GUEST_TASKS = HYPERVISOR / "tasks" / "guest.yml"
FIREWALL_TASKS = ROLES / "setup" / "initial_setup" / "tasks" / "network.yml"

CIDR_VAR = "staging_net_cidr"
LAN_VAR = "lan_subnet"
POD_CIDR_VAR = "k3s_pod_cidr"
SERVICE_CIDR_VAR = "k3s_service_cidr"
FILTER_NAME_VAR = "hypervisor_staging_nwfilter_name"


def _load_host_vars(host: str):
    return yaml.safe_load((HOST_VARS / f"{host}.yml").read_text()) or {}


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

    Too narrow was the live defect: until 2026-08-28 this fenced only lan_subnet, and the
    guest read prod's unauthenticated Longhorn API on a ClusterIP the LAN rule cannot cover.
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
    """The rejecting half, driving the real verdict function on the real pre-fix filter.

    This is the exact XML the role shipped until 2026-08-28 — one rule, lan_subnet only.
    A check that could not tell it apart from the current filter is the check that let the
    guest read prod's Longhorn API for a day.
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
    k3s_defaults = yaml.safe_load(K3S_DEFAULTS.read_text())
    dns_ip = ipaddress.ip_address(k3s_defaults["k3s_cluster_dns_ip"])
    assert dns_ip in service_cidr, (
        f"{SERVICE_CIDR_VAR} is {service_cidr}, which does not contain k3s_cluster_dns_ip "
        f"{dns_ip} from {K3S_DEFAULTS}. One of the two was changed without the other, and "
        f"the fence is around a range the cluster does not use."
    )


def test_fencing_the_clusters_own_ranges_still_assumes_a_single_staging_node():
    """The trade-off the CIDR rules make, tied to the fact that would end it.

    The pod and Service CIDRs are k3s defaults, so STAGING's ranges are the same two. Dropping
    them is safe only because staging's own pod and Service traffic is delivered on the guest's
    internal cni0 and by its own kube-proxy rules, never crossing the tap device this filter
    attaches to. A second staging node would put pod-to-pod traffic on the wire, where the /16
    drop would break it — so the fence would then need a source- or interface-scoped exception.

    daniel-stage carrying no k3s_agent_node_ips override is what says that has not happened.
    """
    staging_agents = _load_host_vars("daniel-stage").get(
        "k3s_agent_node_ips",
        yaml.safe_load(K3S_DEFAULTS.read_text())["k3s_agent_node_ips"],
    )
    assert not staging_agents, (
        f"daniel-stage now declares agent nodes {staging_agents}, so staging is no longer a "
        f"single node. Pod-to-pod traffic crosses the tap device the fence attaches to, and "
        f"the {POD_CIDR_VAR}/{SERVICE_CIDR_VAR} drop rules in {NWFILTER_TEMPLATE} will break "
        f"it. Scope those rules before adding the node."
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


def _probe_url_host(url: str):
    return ipaddress.ip_address(urlparse(url).hostname)


def test_the_probe_dials_the_service_cidr_it_now_fences():
    """A rule with no probe leg is a rule nothing has ever seen fire.

    The checks above read the template; only the probe reads the running host, and the Service
    CIDR is the range the LAN-only fence missed.
    """
    service_cidr = ipaddress.ip_network(_all_vars()[SERVICE_CIDR_VAR])
    probed = _probe_url_host(staging_egress_probe.LONGHORN_PROBE_URL)
    assert probed in service_cidr, (
        f"the probe's cluster target {probed} is not inside {SERVICE_CIDR_VAR} "
        f"({service_cidr}), so a run of it says nothing about the Service-CIDR drop rule."
    )


def test_the_probe_does_not_dial_an_address_that_already_answers_nothing():
    """The rejecting half, and the mistake it rejects was the obvious first draft.

    k8s_registry_cluster_ip and dns_k8s_cluster_ip are the two ClusterIPs pinned in inventory,
    so they are what a probe author reaches for. Both were measured from inside the guest on
    2026-08-28 returning 000 BEFORE any cluster rule existed — the registry behind an ingress
    NetworkPolicy, the DNS Service on a port nothing forwards. Either would read as a held
    fence on the day it shipped and could never go red afterwards.
    """
    all_vars = _all_vars()
    launderers = {all_vars["k8s_registry_cluster_ip"], all_vars["dns_k8s_cluster_ip"]}
    probed = str(_probe_url_host(staging_egress_probe.LONGHORN_PROBE_URL))
    assert probed not in launderers, (
        f"the probe dials {probed}, which answers nothing from the guest with or without the "
        f"fence. Pick a target measured reachable from inside the guest while unfenced."
    )


def test_every_unpinned_probe_target_carries_a_control_leg():
    """An allocated address that moved answers nothing, which reads exactly like a fence.

    The cluster targets are not read from inventory, so nothing stops them going stale in
    place. Their second dial from daniel-server is what turns that into exit 2.
    """
    _, targets = staging_egress_probe._targets()
    inventory_backed = {
        "INTERNET",
        "PRODVIP",
        "K3SAPI",
        "WGEASY",
        "HOSTKUBELET",
        "LANKUBELET",
    }
    uncontrolled = sorted(
        label
        for label, _command, control in targets
        if label not in inventory_backed and not control
    )
    assert not uncontrolled, (
        f"probe targets {uncontrolled} are neither read from inventory nor given a host-side "
        f"control command, so a stale address among them would report a passing fence."
    )
