#!/usr/bin/env python3
"""Tests for postflight.py — the ansible/README.md §9 verifier.

The point of the script is that each §9 step fails SILENTLY in production, so the
thing worth testing is that a bad response is reported as FAIL rather than passing
through. HTTP and Docker are injected out, so these are hermetic.

Run: uv run pytest scripts/diagnostics/tests/test_postflight.py
"""

import json
import os
import socket
import sys

import pytest

# `scripts/diagnostics` is deliberately absent from `pythonpath` in pyproject.toml, so this
# module puts its own parent directory on `sys.path` — the insert every sibling here carries.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import postflight


@pytest.fixture(autouse=True)
def stub_host(monkeypatch):
    """Every workload resolves, and every secret decrypts to a placeholder.

    The host is daniel-box, the node postflight runs on: `host_cluster()` derives the cluster
    from the hostname, and a CI runner's is in no known cluster. Returns the (name, cluster)
    pairs the ClusterIP resolver was asked for.
    """
    resolved = []
    monkeypatch.setattr(socket, "gethostname", lambda: "daniel-box")
    monkeypatch.setattr(
        postflight.health_docker,
        "resolve_service_ip",
        lambda name, cluster: (resolved.append((name, cluster)), "10.0.0.1")[1],
    )
    monkeypatch.setattr(postflight, "secret", lambda name: (f"<{name}>", ""))
    # check_ha_token reaches HA via core.ha_base(), which decrypts the domain from
    # SOPS — stub it so no test needs the age key (CI has none). Same for the cluster
    # prometheus route the Kuma checks query since the PG1 scrape port.
    monkeypatch.setattr(postflight.core, "ha_base", lambda: "https://ha.test")
    monkeypatch.setattr(
        postflight.core,
        "k8s_endpoint",
        lambda h: (f"https://{h}.test", f"{h}.test:443:10.0.0.240"),
    )
    return resolved


# Collection-time aliases: @parametrize is evaluated at import, before a test body runs.
OK_, FAIL_ = postflight.OK, postflight.FAIL
# The unstubbed resolver, taken before the autouse fixture replaces it.
real_resolve_service_ip = postflight.health_docker.resolve_service_ip


def respond(monkeypatch, status, body=""):
    """Stub `core.get_status()`. `status` may instead be a callable for a per-URL reply.

    One patch point for both shapes on purpose: a second `monkeypatch.setattr(postflight.core,
    "get_status", ...)` elsewhere in this file is the same seam patched twice, and the repo
    ratchets on that count.
    """
    reply = status if callable(status) else lambda *a, **kw: (status, body)
    monkeypatch.setattr(postflight.core, "get_status", reply)


def only_checks(monkeypatch, checks):
    """Run `main()` over `checks` alone, so a test isn't at the mercy of the real registry."""
    monkeypatch.setattr(postflight, "CHECKS", checks)


def missing_secret(monkeypatch, name, err=""):
    """Every secret decrypts except `name`, which is absent."""
    monkeypatch.setattr(
        postflight, "secret", lambda n: ("", err) if n == name else ("x", "")
    )


def targets_body(*targets):
    return json.dumps({"data": {"activeTargets": list(targets)}})


def target(job, health="up", last_error=""):
    return {"labels": {"job": job}, "health": health, "lastError": last_error}


def _alert_plane(monkeypatch, up, watchdog):
    """Run check_alert_plane against `up{job="alertmanager"}` and Watchdog result vectors."""

    def reply(url, *a, **kw):
        result = watchdog if "Watchdog" in url else up
        return 200, json.dumps({"data": {"result": result}})

    respond(monkeypatch, reply)
    return postflight.check_alert_plane()


def test_alert_plane_with_alertmanager_up_and_watchdog_firing_is_clean(monkeypatch):
    status, detail = _alert_plane(
        monkeypatch, [{"value": [0, "1"]}], [{"value": [0, "1"]}]
    )
    assert status == postflight.OK
    assert "Watchdog firing" in detail


def test_alert_plane_without_the_watchdog_is_flagged(monkeypatch):
    """An empty ALERTS is also what a dead rule evaluator returns."""
    status, detail = _alert_plane(monkeypatch, [{"value": [0, "1"]}], [])
    assert status == postflight.FAIL
    assert "Watchdog is not firing" in detail


def test_alert_plane_with_alertmanager_unscraped_is_flagged(monkeypatch):
    status, detail = _alert_plane(monkeypatch, [], [{"value": [0, "1"]}])
    assert status == postflight.FAIL
    assert "does not reach Alertmanager" in detail


def test_kuma_scrape_down_reports_the_scrape_error(monkeypatch):
    # up{job="uptime-kuma"} == 0 — since the PG1 port the check reads the up series
    # through the cluster query route (the targets API isn't admitted by it).
    body = json.dumps({"data": {"result": [{"value": [0, "0"]}]}})
    respond(monkeypatch, 200, body)
    status, detail = postflight.check_kuma_scrape()
    assert status == postflight.FAIL
    assert "prometheus_kuma_api_key" in detail


def test_kuma_scrape_absent_target_skips(monkeypatch):
    """A cluster that doesn't scrape Kuma hasn't failed the step — it doesn't have it."""
    respond(monkeypatch, 200, json.dumps({"data": {"result": []}}))
    assert postflight.check_kuma_scrape()[0] == postflight.SKIP


def test_arr_key_mismatch_fails(monkeypatch):
    respond(monkeypatch, 401)
    status, detail = postflight.check_arr_key("sonarr")
    assert status == postflight.FAIL
    assert "sonarr_api_key" in detail


def test_arr_key_ok(monkeypatch):
    respond(monkeypatch, 200, "{}")
    assert postflight.check_arr_key("radarr")[0] == postflight.OK


def test_the_arr_key_check_asks_for_the_path_its_route_admits_is_clean(monkeypatch):
    """Each *arr is asked for the path its `-monitoring` IngressRoute admits.

    `get_via_service` falls back to the Traefik route whenever the ClusterIP does not answer,
    which is every run where the pod is on the other node. A check that asked for
    `/api/<ver>/system/status`, which is on no monitoring route's PathPrefix, would fall
    through to the app's own Authelia'd route and 302 — §9.3 would be structurally SKIP. The
    route side is guarded by
    ansible/tests/k8s/test_arr_monitoring_routes_admit_postflight.py.
    """
    seen = []

    def record(url, header=None, timeout=None, resolve=None):
        seen.append(url)
        return 200, "{}"

    respond(monkeypatch, record)
    for app in ("sonarr", "radarr", "prowlarr"):
        assert postflight.check_arr_key(app)[0] == postflight.OK
    # An exact list, not a subset check: a dropped app would leave a subset assertion passing.
    assert seen == [
        "http://10.0.0.1:8989/api/v3/queue",
        "http://10.0.0.1:7878/api/v3/queue",
        "http://10.0.0.1:9696/api/v1/indexer",
    ]


def test_asking_for_a_path_no_route_admits_is_flagged(monkeypatch):
    """The rejecting half: a path outside the route's prefix reaches Authelia, not the app.

    Rendered as the SKIP `_forward_auth_intercepted` produces, so the failure this guards
    against is a check that reports nothing rather than a check that reports wrongly. The
    assertion is on the response arm, because that is the only thing postflight can observe —
    which is exactly why the path itself is pinned by the test above and by the route guard.
    """

    def only_the_monitored_path(url, header=None, timeout=None, resolve=None):
        return (200, "{}") if url.endswith("/api/v1/indexer") else (302, "")

    respond(monkeypatch, only_the_monitored_path)
    monkeypatch.setitem(
        postflight.ARR_MONITORED_PATH, "prowlarr", "/api/v1/system/status"
    )
    status, detail = postflight.check_arr_key("prowlarr")
    assert status == postflight.SKIP
    assert "forward-auth" in detail


def test_an_unreachable_arr_skips_rather_than_blaming_the_key(monkeypatch):
    """`get()` returns status 0 when curl itself failed, which is a placement fact.

    Reporting it as FAIL read "prowlarr_api_key doesn't match the app's own key" while the
    key was fine — the pod was on the other node, whose NetworkPolicy admits no ipBlock for
    this host. That message sends someone to rotate a working credential.
    """
    respond(monkeypatch, 0, "curl: (7) Failed to connect")
    status, detail = postflight.check_arr_key("prowlarr")
    assert status == postflight.SKIP
    assert "unreachable" in detail
    assert "api_key" not in detail


def test_an_unreachable_jellyfin_skips_too(monkeypatch):
    """The same branch, because fixing only the *arr path would leave the sibling wrong."""
    respond(monkeypatch, 0, "curl: (7) Failed to connect")
    assert postflight.check_jellyfin_key()[0] == postflight.SKIP


def test_the_resolver_reads_a_clusterip_not_a_docker_bridge_ip(monkeypatch):
    """Docker is gone from both cluster nodes, so a bridge-IP lookup raises FileNotFoundError.

    A bridge-IP lookup would make every check reaching a workload directly report the
    FileNotFoundError as the check's own result.
    """
    seen = []
    monkeypatch.setattr(
        postflight.health_docker.core, "k8s_namespace", lambda: "homelab"
    )

    class Result:
        returncode, stdout, stderr = 0, "10.43.0.9\n", ""

    monkeypatch.setattr(
        postflight.health_docker,
        "kubectl",
        lambda cluster, *args, **kw: (seen.append(args), Result())[1],
    )
    assert real_resolve_service_ip("sonarr") == "10.43.0.9"
    assert "docker" not in seen[0]
    assert "service" in seen[0]


def on_host(monkeypatch, hostname):
    """Run the checks as if on `hostname`, over the fixture's daniel-box."""
    monkeypatch.setattr(socket, "gethostname", lambda: hostname)


def test_the_service_ip_read_names_the_cluster_this_host_stands_in(
    monkeypatch, stub_host
):
    """The read took lib.kubectl's `prod` default whichever node ran postflight.

    The invoker refuses on a mismatch, so on daniel-stage the check raised `WrongCluster`
    rather than reading about the wrong cluster — but postflight never named its own. The
    rejecting half is the pre-fix shape: a stage node handing `prod` to the resolver.
    """
    on_host(monkeypatch, "daniel-stage")
    assert postflight.service_ip("sonarr") == "10.0.0.1"
    assert stub_host == [("sonarr", "stage")]


def test_the_service_ip_read_skips_on_a_host_in_no_known_cluster(monkeypatch):
    """The Pi is a node of neither cluster, so a kubectl read has nothing to ask.

    `Skip`, which the runner renders as a SKIP line, rather than a `MissingKubectl` it would
    count as the check's own failure. test_postflight_runner.py has the end-to-end line.
    """
    on_host(monkeypatch, "daniel-pi")
    with pytest.raises(
        postflight.Skip, match="daniel-pi is a node of no known cluster"
    ):
        postflight.service_ip("sonarr")


def test_jellyfin_key_mismatch_fails(monkeypatch):
    respond(monkeypatch, 401)
    assert postflight.check_jellyfin_key()[0] == postflight.FAIL


def test_ha_token_rejected_fails(monkeypatch):
    respond(monkeypatch, 401)
    assert postflight.check_ha_token("claude_ha_token")[0] == postflight.FAIL


def test_ha_token_missing_from_sops_fails(monkeypatch):
    missing_secret(monkeypatch, "homepage_ha_token", "not found")
    respond(monkeypatch, 200)
    assert postflight.check_ha_token("homepage_ha_token") == (
        postflight.FAIL,
        "not found",
    )


def test_authelia_missing_oidc_material_fails(monkeypatch):
    respond(monkeypatch, 200, json.dumps({"status": "OK"}))
    missing_secret(monkeypatch, "authelia_oidc_hmac_secret")
    status, detail = postflight.check_authelia()
    assert status == postflight.FAIL
    assert "authelia_oidc_hmac_secret" in detail


def test_an_unreachable_authelia_skips_rather_than_reporting_an_outage(monkeypatch):
    """The accept half.

    A ClusterIP that does not answer this node is a placement fact — Authelia's pod is on the
    other node. Reporting it as "Authelia is not serving" would be a false outage on the most
    load-bearing service in the fleet.
    """
    respond(monkeypatch, 0, "curl: (7) Failed to connect to 10.43.0.9 port 9091")
    status, detail = postflight.check_authelia()
    assert status == postflight.SKIP
    assert "unreachable from this host" in detail
    assert "not serving" not in detail


def test_authelia_serving_an_error_still_fails(monkeypatch):
    """The reject half: a real non-200 is an outage and must not be softened to SKIP."""
    respond(monkeypatch, 503)
    status, detail = postflight.check_authelia()
    assert status == postflight.FAIL
    assert "not serving" in detail


def test_authelia_oidc_material_is_checked_on_a_node_it_cannot_reach(monkeypatch):
    """The SOPS read needs no network, so the unreachable arm must not skip past it."""
    respond(monkeypatch, 0, "curl: (7) Failed to connect")
    missing_secret(monkeypatch, "authelia_oidc_hmac_secret")
    status, detail = postflight.check_authelia()
    assert status == postflight.FAIL
    assert "authelia_oidc_hmac_secret" in detail


def test_the_route_hostname_comes_from_inventory_not_the_service_name():
    """Authelia's route is `auth`, and a pin aimed at `authelia.local.<domain>` reaches nothing.

    Non-vacuity: this asserts the two named services whose hostname does and does not differ
    from the service name, so the lookup silently returning its argument — an empty
    containers_list, a renamed key — fails here rather than passing on the fallback.
    """
    assert postflight.route_host("authelia") == "auth"
    assert postflight.route_host("jellyfin") == "jellyfin"


def test_an_unanswered_clusterip_falls_back_to_the_traefik_route(monkeypatch):
    """Postflight runs on daniel-box only, so a pod on daniel-server had no asker.

    The ClusterIP answers only a caller on the pod's own node — each workload's NetworkPolicy
    admits pod selectors and no ipBlock for the node — which made §9.5 and §9.3 structurally
    SKIP rather than ever reporting. The route reaches either node.
    """
    seen = []

    def answer(url, header=None, timeout=postflight.core.DEFAULT_TIMEOUT, resolve=None):
        seen.append((url, resolve))
        if url.startswith("http://10."):  # the ClusterIP attempt
            return 0, "curl: (7) Failed to connect"
        return 200, json.dumps({"status": "OK"})

    respond(monkeypatch, answer)
    status, detail = postflight.check_authelia()
    assert status == postflight.OK
    assert "healthy (OK)" in detail
    # The fallback went to the route name with a --resolve pin, not to the ClusterIP again.
    assert seen[-1] == ("https://auth.test/api/health", "auth.test:443:10.0.0.240")


def test_a_forward_auth_redirect_skips_rather_than_blaming_the_key(monkeypatch):
    """An Authelia 302 fires in the middleware, so the app never saw the request.

    The *arr routes carry `use_authelia: true` and their monitoring routes pin ClientIP to
    daniel-server, so both 302 for this host. Letting that reach the tail arm would read
    `HTTP 302 — prowlarr_api_key doesn't match`, sending someone to rotate a working
    credential: strictly worse than a SKIP.
    """
    respond(monkeypatch, 302)
    status, detail = postflight.check_arr_key("prowlarr")
    assert status == postflight.SKIP
    assert "forward-auth" in detail
    assert "doesn't match" not in detail
