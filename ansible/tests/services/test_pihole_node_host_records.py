"""Pi-hole answers for the cluster nodes' own hostnames, each at that node's own address.

Nothing else on the LAN does. A node reaches its peer through a client-side alias in an SSH
client config, and that file is deliberately absent from all three homelab hosts, so without
these records `daniel-box` resolves nowhere from daniel-server. otel-sweep-watch then reports
`box: unreachable (Could not resolve hostname daniel-box)` on every run it makes there — a
daily false finding on a check that only works if it is believed.

The bare name is the half that matters. otel-sweep addresses its machines from a fixed enum
holding `daniel-box` and `daniel-server`, deliberately not caller-configurable, so a
`.lan`-only record would leave that tool exactly as broken as no record at all.

Asserted on the RENDERED dnsmasq config, with the three hosts' `host_vars` handed in as the
play's `hostvars` fact. The whole-tree harness (`rendered_texts()`) stubs `hostvars`, so a
guard built on it sees the NAMES and a placeholder wherever an address belongs; rendering this
one template with the real inventory gives the name-to-address PAIRING, which is the property
worth asserting. A scan of the template source cannot see that pairing at all: swapping the two
nodes' lookups —

    host-record=daniel-box.lan,daniel-box,{{ hostvars['daniel-server'].server_ip }}
    host-record=daniel-server.lan,daniel-server,{{ hostvars['daniel-box'].server_ip }}

— still puts each host's `hostvars[...].server_ip` somewhere on some `host-record=` line, so a
per-host regex over the source passes while every caller of either name reaches the wrong node.

Run: uv run pytest ansible/tests/services/test_pihole_node_host_records.py
"""

from functools import lru_cache

from _k8s_render import render_role_template
from lib.repo_paths import HOST_VARS
from validate.k8s_manifests import load_yaml

# The census this file asserts over, named rather than derived. A test that globbed for
# `host-record=` lines and asserted over whatever it found would pass on an empty set the
# moment the directive were renamed, which is a failure a count cannot see.
REQUIRED = ("daniel-box", "daniel-server")

# Every host whose address a `host-record=` line carries. daniel-pi is in the render but not in
# REQUIRED: it answers under `.lan` only, which is the convention it set.
HOSTS = ("daniel-pi", "daniel-box", "daniel-server")

# One TEST-NET-3 address per host (RFC 5737, never routable), for the test that moves a host's
# inventory address and asserts which records follow it. Distinct per host so a record that
# followed the WRONG host's entry reads as a crossed pairing rather than as a pass.
SENTINELS = {host: f"203.0.113.{n}" for n, host in enumerate(HOSTS, start=1)}

CONFIGMAP = "configmap.yaml.j2"


def host_records(conf: str) -> dict[str, str]:
    """name -> address for every name a `host-record=` line answers for.

    One directive may carry several names before the address, which is exactly how a bare name
    and its `.lan` alias share a line. Pure over the text, so the accept/reject pair below can
    hand it a line the tree does not contain.
    """
    records: dict[str, str] = {}
    for line in conf.splitlines():
        line = line.strip()
        if not line.startswith("host-record="):
            continue
        fields = [f for f in line[len("host-record=") :].split(",") if f]
        if len(fields) < 2:
            continue
        *names, address = fields
        for name in names:
            records[name] = address
    return records


def _hostvars(address_overrides: dict[str, str] | None = None) -> dict[str, dict]:
    """`host_vars/` for the three hosts as a play's `hostvars` fact, one address replaceable."""
    hostvars = {host: load_yaml(HOST_VARS / f"{host}.yml") for host in HOSTS}
    for host, address in (address_overrides or {}).items():
        hostvars[host] = {**hostvars[host], "server_ip": address}
    return hostvars


def rendered_records(address_overrides: dict[str, str] | None = None) -> dict[str, str]:
    """The records Pi-hole's ConfigMap carries, rendered at the given inventory addresses.

    Raises:
        AssertionError: the render carries no `host-record=` line at all. Every assertion
            below is a membership or equality test against this mapping, and an empty one
            would fail them while naming the wrong cause.
    """
    conf = render_role_template(
        "pihole", CONFIGMAP, {"hostvars": _hostvars(address_overrides)}
    )
    records = host_records(conf)
    assert records, (
        f"pihole/{CONFIGMAP} rendered no host-record= line — the census is empty, so nothing "
        "below is checking the records"
    )
    return records


@lru_cache(maxsize=1)
def records_at_inventory_addresses() -> dict[str, str]:
    """The records as the cluster deploys them, rendered once for the whole module."""
    return rendered_records()


# --- the parser, with one input it must accept and one it must reject -----------------


def test_a_record_line_maps_every_name_before_the_address():
    assert host_records("host-record=daniel-box.lan,daniel-box,10.0.0.215\n") == {
        "daniel-box.lan": "10.0.0.215",
        "daniel-box": "10.0.0.215",
    }


def test_a_line_that_is_not_a_host_record_is_ignored():
    """`server=` and `address=` lines sit in the same file and carry the same comma shape."""
    assert host_records("server=/lan/10.0.0.1\naddress=/foo.lan/10.0.0.2\n") == {}


# --- applied to the rendered config ---------------------------------------------------


def test_both_cluster_nodes_answer_under_their_bare_hostname():
    records = records_at_inventory_addresses()
    for host in REQUIRED:
        assert host in records, (
            f"{host} has no host-record, so it resolves nowhere on the LAN and every tool "
            "addressing it by name fails on name resolution rather than on reachability"
        )


def test_the_lan_alias_is_present_too_but_is_not_what_the_sweep_uses():
    # The rejecting half of the pair above, and the reason both halves exist: a record
    # written only as `daniel-box.lan` satisfies the naming convention daniel-pi sets while
    # leaving otel-sweep's fixed enum resolving nothing. Asserting the bare name alone would
    # not catch a later tidy-up that moved these under `.lan` only, and asserting the alias
    # alone would not catch the otel-sweep failure.
    records = records_at_inventory_addresses()
    for host in REQUIRED:
        assert f"{host}.lan" in records, (
            f"{host}.lan should follow the daniel-pi convention"
        )


def test_each_hosts_records_follow_that_hosts_own_inventory_entry():
    # A literal address here would answer correctly today and keep answering after a node
    # moved, which is worse than not answering at all — a stale A record sends every caller to
    # whatever now holds that address. Moving one host's `server_ip` and asserting which
    # records move covers that, and covers the crossed pairing a source scan cannot see.
    for host in HOSTS:
        sentinel = SENTINELS[host]
        records = rendered_records({host: sentinel})
        own = [f"{host}.lan"] + ([host] if host in REQUIRED else [])
        for name in own:
            assert records.get(name) == sentinel, (
                f"{name} answers {records.get(name)!r} after {host}'s server_ip moved to "
                f"{sentinel} — its address is typed in, or taken from another host's entry"
            )
        crossed = {
            name: address
            for name, address in records.items()
            if address == sentinel and name not in own
        }
        assert not crossed, (
            f"{crossed} answer with {host}'s address — these records read "
            f"hostvars['{host}'].server_ip and should read their own host's"
        )


def test_the_census_reads_real_directives():
    # Non-vacuity on a record nothing else here touches, so a parser that quietly stopped
    # matching the directive cannot read as a clean tree.
    # Exact match, not `in`: see ansible/tests/repo/test_census_rows_python.py (row `no-host-shaped-membership-literal`)
    records = records_at_inventory_addresses()
    assert any(name == "daniel-pi.lan" for name in records), (
        "the parser stopped reading host-record lines it used to read"
    )
