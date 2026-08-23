"""Reading the inventory: which services are declared, on which platform, and what is stale.

`containers_list` is the source of truth for both planes, so parsing it wrong routes a k8s
workload down the Docker path. The behind-origin marker is the watchdog for a tick that keeps
running while never catching up.
"""

# ansible/roles/setup/gitops_deploy/files/test_deploy_logic.py

from deploy_logic import (
    ChangeSet,
    services_from_changed_paths,
    behind_marker,
    declared_k8s_services,
    declared_services,
    reroute_k8s_services,
    stale_rendered_services,
)


# behind_marker: the "host is parked on an old tree" signal. Its whole value is the timestamp —
# presence alone is normal (a push is behind for one tick), so these pin the clock semantics.


def test_behind_marker_cleared_when_caught_up():
    assert behind_marker(False, "originX", "originW 100.0", now=200.0) is None


def test_behind_marker_stamps_now_on_first_tick_behind():
    assert behind_marker(True, "originX", None, now=200.0) == "originX 200.0"


def test_behind_marker_keeps_first_seen_across_ticks():
    # Still behind 10 min later: the age must keep growing, not reset.
    assert behind_marker(True, "originX", "originX 200.0", now=800.0) == "originX 200.0"


def test_behind_marker_keeps_first_seen_when_origin_advances():
    # A new push while still stuck refreshes the SHA but must NOT restart the clock — otherwise a
    # steady trickle of pushes to a permanently-stuck host never trips the age threshold.
    assert behind_marker(True, "originZ", "originX 200.0", now=800.0) == "originZ 200.0"


def test_behind_marker_restamps_when_marker_unparseable():
    assert behind_marker(True, "originX", "garbage", now=200.0) == "originX 200.0"


# --- stale-compose watchdog (2nd occurrence of the trap -> machine check) ---------------


def test_declared_services_parses_containers_list_names():
    text = (
        "containers_list:\n"
        "  - name: traefik\n"
        "    port: 8080\n"
        "  - name: monitor-bridge\n"
        "    port: false\n"
        "  # kopia RETIRED 2026-08-10\n"
        "      - name: not-a-service-deeper-indent\n"
    )
    assert declared_services(text) == {"traefik", "monitor-bridge"}


# Platform-aware (2026-08-13): declared_services used to match `- name:` regardless of platform,
# so a platform: k8s entry counted as "declared" here even though deploy.yml's DOCKER play never
# renders a compose for it. A leftover rendered containers/<svc>/ dir for a service that migrated
# to k8s would then phantom-gate as "declared" instead of being flagged stale.
def test_declared_services_skips_platform_k8s_entries():
    text = (
        "containers_list:\n"
        "  - name: crowdsec\n"
        "    platform: k8s\n"
        "    port: 8080\n"
        "  - name: traefik\n"
        "    port: 8080\n"
    )
    assert declared_services(text) == {"traefik"}


def test_declared_services_platform_docker_explicit_is_still_declared():
    text = "containers_list:\n  - name: traefik\n    platform: docker\n    port: 8080\n"
    assert declared_services(text) == {"traefik"}


def test_declared_services_last_entry_platform_k8s_with_no_trailing_entry():
    # The k8s entry is the LAST one in the file (no following `- name:` to bound its block on) —
    # the (?=^  - name: |\Z) lookahead must still terminate the block at EOF.
    text = "containers_list:\n  - name: traefik\n    port: 8080\n  - name: authelia\n    platform: k8s\n    port: 9091\n"
    assert declared_services(text) == {"traefik"}


# --- declared_k8s_services / reroute_k8s_services -----------------------------------------
# A path under ansible/roles/containers/<svc>/{templates,files}/ maps to <svc> by NAME ALONE
# (services_from_changed_paths), with no knowledge of which platform THIS host actually runs
# that service under. wg-easy is a real case: a Docker role (used by daniel-pi), but
# platform: k8s on daniel-box — a template-only push there used to deploy `--tags wg-easy`,
# which resolves to deploy.yml's K8S play, not the Docker one _ACTIVE_CONFIG assumed: an
# idempotent no-op whose health gate is silently skipped too (containers_for() renders no
# compose for a k8s entry). reroute_k8s_services moves such a match into cs.k8s instead, so it
# gets the same defer-and-alert a direct ansible/roles/k8s/** change gets.
def test_declared_k8s_services_parses_platform_k8s_entries():
    text = (
        "containers_list:\n"
        "  - name: wg-easy\n"
        "    platform: k8s\n"
        "    port: 51821\n"
        "  - name: traefik\n"
        "    port: 8080\n"
    )
    assert declared_k8s_services(text) == {"wg-easy"}


def test_declared_k8s_services_excludes_docker_entries():
    text = "containers_list:\n  - name: traefik\n    port: 8080\n"
    assert declared_k8s_services(text) == set()


def test_reroute_k8s_services_moves_matched_service_to_k8s():
    cs = services_from_changed_paths(
        ["ansible/roles/containers/wg-easy/templates/docker-compose.yml.j2"]
    )
    assert cs.services == {"wg-easy"}
    rerouted = reroute_k8s_services(cs, {"wg-easy"})
    assert rerouted.services == set()
    assert rerouted.k8s == {"wg-easy"}


def test_reroute_k8s_services_leaves_docker_services_alone():
    cs = services_from_changed_paths(
        ["ansible/roles/containers/cadvisor/templates/docker-compose.yml.j2"]
    )
    rerouted = reroute_k8s_services(cs, {"wg-easy"})
    assert rerouted.services == {"cadvisor"}
    assert rerouted.k8s == set()


def test_reroute_k8s_services_only_moves_the_matched_subset():
    cs = ChangeSet(services={"cadvisor", "wg-easy"})
    rerouted = reroute_k8s_services(cs, {"wg-easy"})
    assert rerouted.services == {"cadvisor"}
    assert rerouted.k8s == {"wg-easy"}


def test_reroute_k8s_services_merges_into_existing_k8s_set():
    # A single push can carry both a direct ansible/roles/k8s/** change and a containers/<svc>/
    # template for a service that's k8s on this host — both must land in cs.k8s.
    cs = ChangeSet(services={"wg-easy"}, k8s={"authelia"})
    rerouted = reroute_k8s_services(cs, {"wg-easy"})
    assert rerouted.services == set()
    assert rerouted.k8s == {"wg-easy", "authelia"}


def test_stale_rendered_services_flags_only_undeclared_dirs():
    assert stale_rendered_services(
        ["traefik", "kopia", "tempo"], {"traefik", "monitor-bridge"}
    ) == ["kopia", "tempo"]


def test_stale_rendered_services_empty_when_all_declared():
    assert stale_rendered_services(["traefik"], {"traefik", "monitor-bridge"}) == []
