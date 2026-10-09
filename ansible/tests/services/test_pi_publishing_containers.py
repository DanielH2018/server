#!/usr/bin/env python3
"""The Pi's publishing-container set is derived, and the derivation must not narrow.

monitor-bridge's detached-container arm compares each Pi container's live published ports
against `PI_PUBLISHED_PORTS`, which `env-secret.yaml.j2` renders from daniel-pi's
`containers_list` — every entry with a `port`. The derivation is what keeps the set from
drifting as containers come and go, but a derivation can also silently drop a name it was
written to cover, which is the failure this pins: `selectattr('port', 'defined')` returning
one name instead of two reads as a narrowing nobody sees, and the arm goes blind to whichever
container fell out.

Asserted against the RENDERED Secret, not against the template's text. The value the pod
reads is a `name:port` list built by a `{% for %}` loop, so a source scan can only confirm
that the Jinja is still spelled the way it was spelled — it cannot see which containers come
out, which port each one carries, or that `udp_port` stays out (#3107, #2809). Rendered, the
same value is one string the test parses the way `bridge/config_host.py` parses it.

The shared render context carries the Pi's literal `containers_list` in `hostvars` (#3860),
but this test needs to swap that list for one the inventory does not hold. So it renders the
one template with the three hosts' `host_vars` handed in, the way `_kuma_entities.py` does for
the tiles that index the Pi.

The expected members are named rather than counted. A container legitimately added to or
removed from the Pi changes the expected set here in the same commit that changes the
inventory — that edit is the review point, which is exactly what an unpinned derivation
does not give you.

Run: uv run pytest ansible/tests/services/test_pi_publishing_containers.py
"""

from _helpers import HOST_VARS
from _k8s_render import render_role_template
from lib import yaml_fast
from validate.k8s_manifests import load_yaml

ROLE = "monitor-bridge"
ENV_SECRET = "env-secret.yaml.j2"
ENV_VAR = "PI_PUBLISHED_PORTS"
# The bridge reads the Pi's address from `hostvars` too, and the k8s roles' own variables
# resolve against daniel-box, so all three hosts go in rather than the Pi alone.
HOSTS = ("daniel-pi", "daniel-box", "daniel-server")

# These report a `->` mapping in the live glances payload, on these ports. The port is pinned
# with the name because the arm connects to it: a name watched on the wrong port reads as a
# dead container on every cycle.
EXPECTED_PUBLISHERS = {"alloy": 12345, "wg-easy": 51821}
# These report `ports: ""` permanently. A rule that flagged them would page forever.
EXPECTED_NON_PUBLISHERS = {"docker-proxy", "autoheal", "docker-proxy-lifecycle"}
# wg-easy's WireGuard listener. Deliberately not watched: the arm probes with a TCP connect,
# and there is no equivalent "is anything listening" answer for UDP.
WG_EASY_UDP_PORT = 51822


def _hostvars(pi_containers: list | None = None) -> dict[str, dict]:
    """The inventory's `host_vars/` as a play's `hostvars` fact, Pi containers overridable.

    Passing `pi_containers` renders the template against a Pi that the inventory does not
    describe, which is how the derivation is observed moving.
    """
    found = {host: load_yaml(HOST_VARS / f"{host}.yml") for host in HOSTS}
    if pi_containers is not None:
        found["daniel-pi"] = {**found["daniel-pi"], "containers_list": pi_containers}
    return found


def _pi_containers() -> list:
    return load_yaml(HOST_VARS / "daniel-pi.yml")["containers_list"]


def published(pi_containers: list | None = None) -> dict[str, int]:
    """`PI_PUBLISHED_PORTS` as the pod receives it: container name -> watched port.

    Parsed the way `bridge/config_host.py:_published_ports` parses it, so a value this test
    accepts is a value the bridge watches. A malformed pair is an assertion here rather than
    a skipped entry, because in the pod it is a container that silently stops being probed.
    """
    rendered = render_role_template(
        ROLE, ENV_SECRET, {"hostvars": _hostvars(pi_containers)}
    )
    secret = yaml_fast.safe_load(rendered)
    assert secret.get("kind") == "Secret", (
        f"{ROLE}/{ENV_SECRET} no longer renders a Secret, got {secret.get('kind')!r} — "
        f"every assertion below would read an empty mapping and pass"
    )
    raw = (secret.get("stringData") or {}).get(ENV_VAR)
    assert raw is not None, (
        f"the rendered Secret carries no {ENV_VAR} key, so the detached-container arm watches "
        f"nothing and the census below is vacuous"
    )
    pairs: dict[str, int] = {}
    for pair in raw.split(","):
        if not pair.strip():
            continue
        name, sep, port = pair.partition(":")
        assert sep and port.strip().isdigit(), (
            f"{ENV_VAR} entry {pair!r} is not `name:port`; config_host.py drops it, so that "
            f"container is not watched"
        )
        pairs[name.strip()] = int(port)
    return pairs


def test_every_known_publisher_is_watched_on_its_published_port():
    watched = published()
    for name, port in EXPECTED_PUBLISHERS.items():
        assert watched.get(name) == port, (
            f"{name} should be watched on {port}, the rendered Secret says "
            f"{watched.get(name)!r} — the detached-container arm either no longer watches it "
            f"or connects to the wrong port"
        )


def test_the_known_non_publishers_stay_out():
    # docker-proxy-lifecycle is hardcoded in the Pi's compose and is not in containers_list
    # at all, so it falls out for a second reason; the other two are here by having no port.
    assert not (EXPECTED_NON_PUBLISHERS & set(published()))


def test_the_set_has_not_widened_unreviewed():
    extra = set(published()) - set(EXPECTED_PUBLISHERS)
    assert not extra, (
        "new Pi container(s) %s publish a port — confirm they really do, then add them to "
        "EXPECTED_PUBLISHERS with the port the arm should probe" % sorted(extra)
    )


def test_the_wireguard_udp_port_is_not_watched():
    # The source could only show that `udp_port` is absent from the loop body; the rendered
    # value shows that wg-easy arrives carrying its TCP port and nothing else. A connect to
    # the UDP port would read as a dead container every cycle.
    assert WG_EASY_UDP_PORT not in set(published().values())


def test_the_set_follows_the_inventory_rather_than_a_typed_list():
    """The red-proof half: a hand-typed list renders every assertion above green.

    Both directions, because they fail differently. A list that cannot grow leaves a new
    container unwatched; one that cannot shrink probes a port nothing listens on any more.
    """
    added = published([*_pi_containers(), {"name": "probe-publisher", "port": 65000}])
    assert added.get("probe-publisher") == 65000, (
        f"a Pi container with a `port` did not reach {ENV_VAR}; the value is typed in, not "
        f"derived, and the next container added to the Pi goes unwatched. got {added!r}"
    )
    unported = [
        {key: value for key, value in container.items() if key != "port"}
        for container in _pi_containers()
    ]
    assert published(unported) == {}, (
        f"{ENV_VAR} still names containers after every `port` left the inventory, so it is a "
        f"typed list and the arm probes ports nothing publishes"
    )


def test_a_udp_only_container_does_not_join_the_set():
    """`udp_port` alone must not make a container watched — the arm has no UDP probe."""
    base = published()
    with_udp = published([*_pi_containers(), {"name": "probe-udp", "udp_port": 65001}])
    assert with_udp == base, (
        f"a container publishing only UDP changed the watched set: {with_udp!r} vs {base!r}"
    )
