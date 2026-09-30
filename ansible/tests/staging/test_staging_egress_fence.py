"""Every guest on the staging network must be fenced off the production LAN, by a filter of
the right shape.

Since daniel-stage was retired (#2941) the only guest wearing the fence is the etcd restore
drill's throwaway one, which holds the cluster token and the R2 write credentials for the
whole backup bucket — so the fence matters at least as much as it did.

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

Two halves live here. The first checks the filter's SHAPE and its ATTACHMENT, which is all a
check that never leaves the repo can see: a correct filter that is unattached reads green in
every listing the host offers, and the inert ufw rule passed a whole file of shape tests.

The second half is the reachability gate. It measures the leg the drill orchestrator runs from
INSIDE the guest — the only place a fence's firing can be measured — by rendering that
orchestrator and running the leg under bash against stub dials. It replaces
scripts/diagnostics/staging_egress_probe.py, which ran on demand inside the persistent
daniel-stage guest and was deleted with it (#2941, restored as a drill leg in #2943). The leg
itself runs once per monthly drill, before the guest is handed the cluster token or the R2
write credentials; these tests are what proves its four verdicts can each go red.

The pair that matters most is the two CIDR tests at the end. A fence keyed to a network that
does not contain the guest is not a weaker fence, it is no fence at all, and it reads green
in every listing the host offers.
"""

import ipaddress
import re
import xml.etree.ElementTree as ET

from lib import yaml_fast

from _fence_probe import (
    CONTROL,
    ORCHESTRATOR,
    POD_IP,
    fence_targets_block,
    dialled_addresses,
    host_reaches,
    run_fence,
    state,
)
from _helpers import ALL_VARS, HOST_VARS, ROLES, jinja_env

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
    reservations = re.findall(r"<host\b[^>]*>", NETWORK_TEMPLATE.read_text())
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


def test_a_fence_that_holds_reports_hold_and_records_its_evidence(tmp_path):
    """The input it must ACCEPT: the control answers, every production target is refused.

    This is #2943's Verify-by in miniature — the evidence names each fenced range refused and
    the internet control target reachable.
    """
    verdict, lines = run_fence(
        tmp_path, guest_reachable=[CONTROL], host_reachable=host_reaches()
    )

    assert verdict == "VERDICT hold", verdict
    assert state(lines, "INTERNET").startswith("REACHABLE"), lines
    for line in (l for l in lines if not l.startswith("INTERNET")):
        assert "refused" in line, f"a production target was not refused: {line}"
    assert {l.split()[0] for l in lines} == {
        "INTERNET",
        "PRODVIP",
        "K3SAPI",
        "WGEASY",
        "LONGHORNSVC",
        "PODNET",
    }, f"the evidence does not name the targets this leg is supposed to dial: {lines}"


def test_a_production_target_answering_from_the_guest_aborts_the_run(tmp_path):
    """REJECT: the 2026-08-27 measurement, which is what the fence was added for.

    wg-easy's admin UI is unauthenticated and LAN-only, so reaching it from the guest is the
    leak. It has to abort rather than note it — the next thing the orchestrator does is hand
    that guest the cluster token and the R2 write credentials.
    """
    pi = _load_host_vars("daniel-pi")["server_ip"]
    verdict, lines = run_fence(
        tmp_path,
        guest_reachable=[CONTROL, f"http://{pi}:51821"],
        host_reachable=host_reaches(),
    )

    assert verdict.startswith("FAIL egress fence BROKEN"), verdict
    assert "WGEASY" in verdict, verdict
    assert "Nothing was staged" in verdict, verdict
    assert state(lines, "WGEASY").startswith("REACHABLE"), lines


def test_a_guest_with_no_egress_at_all_is_a_broken_fence_not_a_pass(tmp_path):
    """REJECT: the shape that makes every other assertion here meaningless.

    A rule that lost its destination drops everything, so every production target is refused and
    the run reads like a perfect pass. The control target is the only thing telling the two
    apart, which is why a lost control is a failure rather than a clean sweep.
    """
    verdict, lines = run_fence(tmp_path, guest_reachable=[])

    assert verdict.startswith("FAIL egress fence UNPROVEN"), verdict
    assert "internet control target" in verdict, verdict
    assert state(lines, "INTERNET").startswith("refused"), lines


def test_a_target_that_answers_from_neither_side_is_named_and_not_fatal(tmp_path):
    """A ClusterIP that moved is a probe-maintenance fault, not a leak and not a drill failure.

    Staleness only degrades the meaning of a NEGATIVE: a target answering from inside the guest
    is a leak whether or not it is stale. So the run continues with the label in its verdict,
    and the orchestrator's DECIDED comment carries the trade.
    """
    vip = _all_vars()["k3s_metallb_ingress_vip"]
    verdict, lines = run_fence(
        tmp_path,
        guest_reachable=[CONTROL],
        # Everything daniel-server can still reach EXCEPT the Longhorn ClusterIP, which is what
        # a Service that moved to a new address looks like from this host.
        host_reachable=[CONTROL, f"http://{vip}", POD_IP],
    )

    assert verdict == "VERDICT hold,unproven=LONGHORNSVC", verdict
    assert "stale" in state(lines, "LONGHORNSVC"), lines
    assert "refused" in state(lines, "PRODVIP"), (
        "a target the host itself can still reach was proven, so it must read as refused rather "
        f"than stale: {lines}"
    )


def test_a_dial_whose_tool_is_missing_reads_as_unproven_not_refused(tmp_path):
    """This issue's own bug shape, one level down.

    A guest without `ping` refuses nothing — it cannot dial at all. Counting that as a block is
    how a gate reports a fence it never tested, so it lands in the same `unproven` list as a
    stale address rather than in the refused column.
    """
    verdict, lines = run_fence(
        tmp_path,
        guest_reachable=[CONTROL],
        host_reachable=host_reaches(),
        guest_tools=("curl",),
    )

    assert verdict == "VERDICT hold,unproven=PODNET", verdict
    assert "UNPROVEN" in state(lines, "PODNET"), lines


def test_the_leg_runs_before_the_guest_is_handed_any_credential():
    """Placement is the half of this that cannot be measured from inside the guest.

    A leg that ran after the staging tar would report the leak correctly and a minute too late:
    the cluster token and the R2 write credentials for the whole backup bucket would already be
    on a guest that can reach production.
    """
    body = ORCHESTRATOR.read_text()
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
