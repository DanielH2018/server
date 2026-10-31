#!/usr/bin/env python3
"""The Pi's publishing-container set is derived, and the derivation must not narrow.

observability's `blackbox-pi-ports` scrape job probes one TCP port per Pi container that
publishes one, and `PiPortDead` pages a dead port. The job's targets come from daniel-pi's
`containers_list` and `compose_services` — every entry with a `port` — in
`prometheus.yaml.j2`. The derivation is what keeps the set from drifting as containers come and
go, but a derivation can also silently drop a name it was written to cover, which is the failure
this pins: `published_ports` returning one name instead of two reads as a narrowing nobody sees,
and the rule goes blind to whichever container fell out.

Asserted against the RENDERED `prometheus.yml`, not against the template's text. The targets
come from a `{% for %}` loop, so a source scan can only confirm that the Jinja is still spelled
the way it was spelled — it cannot see which containers come out, which port each one carries,
or that `udp_port` stays out (#3107, #2809). Rendered, the same value is a list of targets the
test parses.

The shared render context carries the Pi's literal `containers_list` in `hostvars` (#3860), but
this test needs to swap that list for one the inventory does not hold. So it deep-copies the
context's `hostvars` and overrides the Pi's two service keys.

The expected members are named rather than counted. A container legitimately added to or
removed from the Pi changes the expected set here in the same commit that changes the
inventory — that edit is the review point, which is exactly what an unpinned derivation
does not give you.

The monitor-bridge `pi_pressure` row read the same set from a `PI_PUBLISHED_PORTS` env key until
M6's batch B8 deleted the row, and this file held that key to the same rules.

Run: uv run pytest ansible/tests/services/test_pi_publishing_containers.py
"""

import copy

from _k8s_render import host_context, render_role_template
from lib import yaml_fast

JOB = "blackbox-pi-ports"

# These listen on these ports. The port is pinned with the name because the probe connects to
# it: a name watched on the wrong port reads as a dead container on every cycle.
EXPECTED_PUBLISHERS = {"alloy": 12345, "wg-easy": 51821}
# These publish no port. A rule that flagged them would page forever.
EXPECTED_NON_PUBLISHERS = {"docker-proxy", "autoheal", "docker-proxy-lifecycle"}
# wg-easy's WireGuard listener. Deliberately not watched: the probe is a TCP connect, and
# there is no equivalent "is anything listening" answer for UDP.
WG_EASY_UDP_PORT = 51822


def _pi_vars() -> dict:
    return host_context()["hostvars"]["daniel-pi"]


def published(
    containers_list: list | None = None, compose_services: list | None = None
) -> dict[str, int]:
    """The Pi job's targets as `service -> port`, in the order Prometheus scrapes them.

    Passing a list renders the template against a Pi that the inventory does not describe, which
    is how the derivation is observed moving. A target that is not `<address>:<port>` is an
    assertion here rather than a skipped entry, because in the pod it is a container that
    silently stops being probed.
    """
    hostvars = copy.deepcopy(host_context()["hostvars"])
    if containers_list is not None:
        hostvars["daniel-pi"]["containers_list"] = containers_list
    if compose_services is not None:
        hostvars["daniel-pi"]["compose_services"] = compose_services
    text = render_role_template(
        "observability", "prometheus.yaml.j2", {"hostvars": hostvars}
    )
    configmap = next(
        doc
        for doc in yaml_fast.safe_load_all(text)
        if doc and doc.get("kind") == "ConfigMap"
    )
    config = yaml_fast.safe_load(configmap["data"]["prometheus.yml"])
    [job] = [j for j in config["scrape_configs"] if j["job_name"] == JOB]
    found: dict[str, int] = {}
    for item in job["static_configs"]:
        for target in item["targets"]:
            _address, sep, port = target.rpartition(":")
            assert sep and port.isdigit(), (
                f"{JOB} target {target!r} is not `address:port`, so that container is not probed"
            )
            found[item["labels"]["service"]] = int(port)
    return found


def test_every_known_publisher_is_watched_on_its_published_port():
    watched = published()
    for name, port in EXPECTED_PUBLISHERS.items():
        assert watched.get(name) == port, (
            f"{name} should be probed on {port}, the rendered job says "
            f"{watched.get(name)!r} — PiPortDead either no longer watches it "
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
        "EXPECTED_PUBLISHERS with the port the probe should connect to" % sorted(extra)
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
    added = published(
        [*_pi_vars()["containers_list"], {"name": "probe-publisher", "port": 65000}]
    )
    assert added.get("probe-publisher") == 65000, (
        f"a Pi container with a `port` did not reach {JOB}; the targets are typed in, not "
        f"derived, and the next container added to the Pi goes unwatched. got {added!r}"
    )
    # Not every `port`: the `alloy-pi` scrape job reads alloy's port from the same list, so a
    # Pi with none at all does not render. Dropping wg-easy's alone shows the same thing.
    pi = _pi_vars()
    unported = published(
        pi["containers_list"],
        [
            {
                key: value
                for key, value in e.items()
                if not (e["name"] == "wg-easy" and key == "port")
            }
            for e in pi["compose_services"]
        ],
    )
    assert unported == {"alloy": 12345}, (
        f"{JOB} still names wg-easy after its `port` left the inventory, so it is a typed "
        f"list and the probe connects to a port nothing publishes. got {unported!r}"
    )


def test_a_udp_only_container_does_not_join_the_set():
    """`udp_port` alone must not make a container watched — the probe has no UDP mode."""
    base = published()
    with_udp = published(
        [*_pi_vars()["containers_list"], {"name": "probe-udp", "udp_port": 65001}]
    )
    assert with_udp == base, (
        f"a container publishing only UDP changed the watched set: {with_udp!r} vs {base!r}"
    )


def test_a_publisher_in_either_key_is_still_watched():
    """The set is the same whichever key holds the entry, and the order is by name.

    Every Pi publisher is in `compose_services` since H5.9, so the rollback shape is the one to
    test: a revert of a move PR puts the entry back in `containers_list`.
    """
    pi = _pi_vars()
    wg_easy = [e for e in pi["compose_services"] if e["name"] == "wg-easy"]
    assert wg_easy, (
        "wg-easy left compose_services, so this test no longer reverts a move"
    )
    reverted = published(
        [*pi["containers_list"], *wg_easy],
        [e for e in pi["compose_services"] if e["name"] != "wg-easy"],
    )
    assert list(reverted.items()) == list(published().items())
    assert list(reverted.items()) == [("alloy", 12345), ("wg-easy", 51821)]
