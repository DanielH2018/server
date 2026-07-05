#!/usr/bin/env python3
"""Invariant: every Docker network a service attaches to (host_vars `containers_list[].networks`)
must be created by docker_install's "Create Docker networks" loop.

Otherwise a service on a brand-new network deploys fine on an EXISTING host (the net is
already there) but fails only on a fresh-host bring-up — this catches it pre-deploy instead.
Service-INTERNAL nets (pihole_internal, scrutiny_internal, crowdsec-db) are declared inside
their own compose and never appear in host_vars, so they're correctly out of scope here.
Mirrors validate_compose_templates' host_vars-driven model.

Run: uv run pytest ansible/tests/test_network_invariant.py
"""

import pathlib
import re

import yaml

_ANSIBLE = pathlib.Path(__file__).resolve().parents[1]

# Hand-rolled routers that Traefik serves but that carry NO HTTP `port` in host_vars, so the
# truthy-`port` "is this routed?" heuristic below would miss them:
#   - terraria: exposed over a dedicated raw-TCP entrypoint (:7777).
#   - authelia: hand-rolls its own HTTP router labels directly (doesn't call the labels() macro,
#     so it has no `port` in host_vars) — the forward-auth portal every use_authelia service needs.
# In both cases the route network is networks[0] and Traefik must join it or the backend is
# unreachable. authelia sits on `proxy` today (which Traefik always joins) so this is a no-op guard
# now — it future-proofs the check against authelia ever being moved onto a dedicated isolation net.
_HANDROLLED_ROUTED = {"terraria", "authelia"}


def _created_networks() -> set[str]:
    all_vars = yaml.safe_load((_ANSIBLE / "inventory/group_vars/all.yml").read_text())
    docker_network = all_vars["docker_network"]
    tasks = yaml.safe_load(
        (_ANSIBLE / "roles/setup/docker_install/tasks/main.yml").read_text()
    )
    loop = next(t["loop"] for t in tasks if t.get("name") == "Create Docker networks")
    # the loop's first item is the Jinja "{{ docker_network }}"; the rest are literals
    return {
        docker_network if i.strip() == "{{ docker_network }}" else i.strip()
        for i in loop
    }


def _referenced_networks() -> dict[str, str]:
    refs: dict[str, str] = {}  # network -> first "host/service" that references it
    for host_file in (_ANSIBLE / "inventory/host_vars").glob("*.yml"):
        host_vars = yaml.safe_load(host_file.read_text()) or {}
        for svc in host_vars.get("containers_list") or []:
            for net in svc.get("networks") or []:
                refs.setdefault(net, f"{host_file.stem}/{svc.get('name')}")
    return refs


def test_every_referenced_network_is_created_by_docker_install():
    created = _created_networks()
    missing = {
        net: where
        for net, where in _referenced_networks().items()
        if net not in created
    }
    assert not missing, (
        "networks referenced in host_vars but NOT created by docker_install's loop "
        "(would fail only on a fresh-host deploy): %s" % missing
    )


def test_docker_install_creates_the_expected_core_networks():
    # guard against the loop being accidentally gutted
    assert {"proxy", "monitoring", "media", "apps"} <= _created_networks()


def _traefik_networks() -> set[str]:
    """The Docker networks the traefik CONTAINER actually joins.

    Traefik's real net set is the literal UNION list in its role compose template, NOT its
    host_vars `containers_list` entry (which carries only `proxy`). The role bundles a second
    service (the crowdsec agent) in the same file, so anchor to the `traefik:` service block and
    read its service-level `networks:` list (4-space key, 6-space `- <net>` items), skipping the
    interspersed comment/blank lines and stopping at the next key (e.g. `ports:`).
    """
    lines = (
        (_ANSIBLE / "roles/containers/traefik/templates/docker-compose.yml.j2")
        .read_text()
        .splitlines()
    )
    start = next(i for i, ln in enumerate(lines) if re.match(r" {2}traefik:\s*$", ln))
    end = next(
        (i for i in range(start + 1, len(lines)) if re.match(r" {2}\S", lines[i])),
        len(lines),
    )
    block = lines[start:end]
    nstart = next(i for i, ln in enumerate(block) if re.match(r" {4}networks:\s*$", ln))
    nets: set[str] = set()
    for ln in block[nstart + 1 :]:
        m = re.match(r" {6}- ([\w-]+)\s*$", ln)
        if m:
            nets.add(m.group(1))
        elif re.match(r"\s*#", ln) or not ln.strip():
            continue  # comment or blank line inside the list
        else:
            break  # first real non-list line (e.g. `    ports:`) ends the block
    return nets


def _routed_route_networks() -> dict[str, str]:
    """{route_network: "host/service"} for every Traefik-routed service on the host that runs
    Traefik.

    A service is Traefik-routed when it emits `labels()` — i.e. it has a truthy `port` (or it's a
    hand-rolled router with no host_vars `port`, see `_HANDROLLED_ROUTED`). The macro binds
    `traefik.docker.network` to `networks[0]`, so ONLY networks[0] is the route network; the rest
    of a service's list are deliberate isolation/reach nets Traefik must NOT be forced onto
    (`mqtt`, `ups`, `codeserver`, `homepage_private`, …). Asserting the full list ⊆ traefik.nets
    would false-positive on exactly those (zigbee2mqtt/home-assistant/homepage/code-server).
    """
    refs: dict[str, str] = {}
    for host_file in (_ANSIBLE / "inventory/host_vars").glob("*.yml"):
        host_vars = yaml.safe_load(host_file.read_text()) or {}
        svcs = host_vars.get("containers_list") or []
        # Only the host that actually runs Traefik (Pi has no traefik → no routing to check).
        if not any(s.get("name") == "traefik" for s in svcs):
            continue
        for svc in svcs:
            name = svc.get("name")
            nets = svc.get("networks") or []
            routed = bool(svc.get("port")) or name in _HANDROLLED_ROUTED
            if name == "traefik" or not routed or not nets:
                continue
            refs.setdefault(nets[0], f"{host_file.stem}/{name}")
    return refs


def test_traefik_parse_is_sane():
    # Guard: a parse regression returning {} would make the invariant below vacuously pass.
    assert {"proxy", "monitoring", "media", "apps", "kopia"} <= _traefik_networks()


def test_traefik_joins_every_routed_network():
    traefik_nets = _traefik_networks()
    missing = {
        net: where
        for net, where in _routed_route_networks().items()
        if net not in traefik_nets
    }
    assert not missing, (
        "routed service(s) whose route network (networks[0]) Traefik does NOT join — the route "
        "would 502 only at request time with no pre-deploy signal. Add the net to the traefik "
        "service's `networks:` in roles/containers/traefik/templates/docker-compose.yml.j2: %s"
        % missing
    )
