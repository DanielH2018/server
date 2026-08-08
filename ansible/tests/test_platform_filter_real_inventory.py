#!/usr/bin/env python3
"""Guards filter_by_platform against the real inventory, not a synthetic list.

deploy.yml runs this filter over every host's containers_list, and daniel-server
has has_gitops: true — a 30-minute timer pulls master and deploys. So a filter
that silently dropped entries would take services down with no manual gate in
between. These tests assert the filter is a no-op for every entry that hasn't
been explicitly migrated, using the inventory files as they actually are.

They stay correct as services migrate: an entry only leaves the docker set by
gaining `platform: k8s`, which is exactly what the assertions check for.

Run: uv run pytest ansible/tests/test_platform_filter_real_inventory.py
"""

from pathlib import Path

import pytest
import yaml

from toposort import filter_by_platform

HOST_VARS = Path(__file__).resolve().parents[1] / "inventory" / "host_vars"


def _host_var_files():
    return sorted(p for p in HOST_VARS.glob("*.yml") if not p.name.startswith("_"))


def _containers(path):
    return (yaml.safe_load(path.read_text()) or {}).get("containers_list") or []


@pytest.mark.parametrize("path", _host_var_files(), ids=lambda p: p.stem)
def test_docker_filter_keeps_every_unmigrated_entry_in_order(path):
    containers = _containers(path)
    expected = [
        c["name"] for c in containers if c.get("platform", "docker") == "docker"
    ]

    got = [c["name"] for c in filter_by_platform(containers, "docker")]

    assert got == expected


@pytest.mark.parametrize("path", _host_var_files(), ids=lambda p: p.stem)
def test_every_entry_lands_in_exactly_one_platform(path):
    containers = _containers(path)

    docker = filter_by_platform(containers, "docker")
    k8s = filter_by_platform(containers, "k8s")

    assert len(docker) + len(k8s) == len(containers), (
        "an entry carries a platform value that is neither docker nor k8s, so "
        "deploy.yml would skip it and no k8s manifest would claim it"
    )


def test_daniel_server_is_still_wholly_docker():
    # The migration HAS started: cloudflare-ddns was cut over to k3s on 2026-08-05 (46 -> 45),
    # then speedtest (45 -> 44), littlelink (44 -> 43), freshrss (43 -> 42) and healthchecks
    # (42 -> 41) the same day, all behind the strangler bridge. Then tempo was ADDED on
    # 2026-08-06 (41 -> 42), the trace sink for Claude Code's spans, and livesync (42 -> 41) and
    # karakeep (41 -> 40) were cut over the same day. karakeep took three helper containers with
    # it that were never separate entries here, so the drop is 1 while four containers left.
    # Then n8n (40 -> 39), which took n8n-runners with it the same way. Then slice 4's B4c
    # cutover (39 -> 34), which moved five AT ONCE — qbittorrent, sonarr, radarr, prowlarr,
    # bazarr — taking wireguard and flaresolverr with them as sidecars that were never their
    # own entries. Five together and not one at a time because they share the hardlink seam:
    # split them across hosts and every import silently becomes a copy. Then jellyfin on
    # 2026-08-08 (34 -> 33), B5, which moved ALONE for the mirror-image reason: it only reads
    # the library, so it shares no seam with the five. Then tdarr the same day (33 -> 32), B6,
    # alone again — no seam either, but it needs the GPU. The count
    # stays hardcoded on purpose — bumping it is the deliberate act that says "a service was
    # retired" (or added), and a drop nobody edited means the default regressed and production
    # services silently stopped being managed.
    #
    # Every remaining entry must still be `docker`. daniel-server does not run k3s and will
    # not until slice 7, so a k8s entry here would be deployed by neither play.
    containers = _containers(HOST_VARS / "daniel-server.yml")

    assert len(containers) == 32
    assert len(filter_by_platform(containers, "docker")) == 32
    assert filter_by_platform(containers, "k8s") == []
