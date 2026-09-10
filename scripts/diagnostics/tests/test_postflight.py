#!/usr/bin/env python3
"""Tests for postflight.py — the ansible/README.md §9 verifier.

The point of the script is that each §9 step fails SILENTLY in production, so the
thing worth testing is that a bad response is reported as FAIL rather than passing
through. HTTP and Docker are injected out, so these are hermetic.

Run: uv run pytest scripts/diagnostics/tests/test_postflight.py
"""

import json
import os
import sys

import pytest

# `scripts/diagnostics` is deliberately absent from `pythonpath` in pyproject.toml, so this
# module puts its own parent directory on `sys.path` — the insert every sibling here carries.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import postflight


@pytest.fixture(autouse=True)
def stub_host(monkeypatch):
    """Every workload resolves, and every secret decrypts to a placeholder."""
    monkeypatch.setattr(postflight, "service_ip", lambda name: "10.0.0.1")
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


# Collection-time aliases: @parametrize is evaluated at import, before a test body runs.
OK_, FAIL_ = postflight.OK, postflight.FAIL


def respond(monkeypatch, status, body=""):
    """Stub `get()`. `status` may instead be a `get`-shaped callable for a per-URL reply.

    One patch point for both shapes on purpose: a second `monkeypatch.setattr(postflight,
    "get", ...)` elsewhere in this file is the same seam patched twice, and the repo ratchets
    on that count.
    """
    reply = status if callable(status) else lambda *a, **kw: (status, body)
    monkeypatch.setattr(postflight, "get", reply)


def only_checks(monkeypatch, checks):
    """Run `main()` over `checks` alone, so a test isn't at the mercy of the real registry."""
    monkeypatch.setattr(postflight, "CHECKS", checks)


def stub_curl(monkeypatch, run):
    """Replace the subprocess `get()` shells out to."""
    monkeypatch.setattr(postflight.subprocess, "run", run)


def missing_secret(monkeypatch, name, err=""):
    """Every secret decrypts except `name`, which is absent."""
    monkeypatch.setattr(
        postflight, "secret", lambda n: ("", err) if n == name else ("x", "")
    )


def targets_body(*targets):
    return json.dumps({"data": {"activeTargets": list(targets)}})


def target(job, health="up", last_error=""):
    return {"labels": {"job": job}, "health": health, "lastError": last_error}


def test_kuma_monitors_ok(monkeypatch):
    body = json.dumps({"data": {"result": [{"value": [0, "118"]}]}})
    respond(monkeypatch, 200, body)
    assert postflight.check_kuma_monitors() == (
        postflight.OK,
        "118 monitors provisioned",
    )


def test_kuma_monitors_empty_result_fails(monkeypatch):
    """No admin -> AutoKuma provisions nothing -> the metric has no series at all."""
    respond(monkeypatch, 200, json.dumps({"data": {"result": []}}))
    status, detail = postflight.check_kuma_monitors()
    assert status == postflight.FAIL
    assert "0 monitors" in detail


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

    Every check reaching a workload directly was dead that way from the 2026-08-14 retirement
    until 2026-08-25, and reported the FileNotFoundError as the check's own result.
    """
    seen = []
    monkeypatch.setattr(
        postflight.health_docker.core, "k8s_namespace", lambda: "homelab"
    )

    class Result:
        returncode, stdout, stderr = 0, "10.43.0.9\n", ""

    monkeypatch.setattr(
        postflight.health_docker.subprocess,
        "run",
        lambda argv, **kw: (seen.append(argv), Result())[1],
    )
    assert postflight.health_docker.resolve_service_ip("sonarr") == "10.43.0.9"
    assert "docker" not in seen[0]
    assert "service" in seen[0]


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
    """The accept half of #1564.

    A ClusterIP that does not answer this node is a placement fact — Authelia's pod is on the
    other node. Reporting it as "Authelia is not serving" was a false outage on the most
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


def test_kuma_drift_reads_its_constants_from_the_module_that_holds_them(monkeypatch):
    """#1562: postflight read four names off `probe`, which holds none of them.

    Exercising the check is the point — a `hasattr` census would pass before and after the
    fix. This raised `AttributeError: module 'probe' has no attribute 'STATIC_MONITORS_PATH'`,
    which the runner reported as FAIL, so a check that never ran read as drift found.
    """
    declared = {
        "sonarr": {"type": "http", "interval": 60, "gated": False, "gate": None}
    }
    status, detail = _drift_over(monkeypatch, declared, {"sonarr"})
    assert status == postflight.OK
    assert isinstance(detail, str)


def _drift_over(monkeypatch, declared, live):
    """Drive check_kuma_drift with `declared` against a live set, returning (status, detail)."""
    body = json.dumps(
        {
            "data": {
                "result": [
                    {"metric": {"monitor_name": n}, "value": [0, "1"]} for n in live
                ]
            }
        }
    )
    respond(monkeypatch, 200, body)
    monkeypatch.setattr(postflight.monitors, "kuma_pod_age_seconds", lambda: 99999)
    # STATIC_MONITORS_PATH is left alone: the real declaration file is tracked, and the parse
    # is patched anyway, so the only thing it supplies here is bytes to read.
    monkeypatch.setattr(
        postflight.monitors, "parse_declared_monitors", lambda text: declared
    )
    return postflight.check_kuma_drift()


GATED_AND_ABSENT = {
    "Off-box etcd Snapshot": {
        "type": "push",
        "interval": 86400,
        "gated": True,
        "gate": "etcd_snapshot_push_token",
    }
}


@pytest.mark.parametrize(
    "gate_is_set, expected, expected_text",
    [
        (True, FAIL_, "Off-box etcd Snapshot: declared, not live"),
        (False, OK_, "genuinely unset, skipped"),
    ],
    ids=["set-and-absent-is-drift", "unset-is-excused"],
)
def test_a_gated_monitors_absence_is_judged_against_its_secret(
    monkeypatch, gate_is_set, expected, expected_text
):
    """#1632's red-proof pair, and the whole point of the section.

    §9.1 passed no gate_states, so `format_kuma_drift` fell through to its excused arm for
    every gated monitor whatever the secret said — seven of them on 2026-09-10, under an [OK].
    A gated monitor is the one nothing else watches, so the drift half could not see the case
    it exists for. Measured 2026-08-22: `etcd_snapshot_push_token` was set, its monitor was not
    live, and the check called that correctly skipped.

    The unset row is not padding: without it the fix is a louder check that cries wolf on
    every gate, and a check that fires on everything is as useless as one that fires on
    nothing.
    """
    monkeypatch.setattr(postflight.monitors, "gate_var_state", lambda var: gate_is_set)
    status, detail = _drift_over(monkeypatch, GATED_AND_ABSENT, set())
    assert status == expected
    assert expected_text in detail


def test_the_route_hostname_comes_from_inventory_not_the_service_name():
    """Authelia's route is `auth`, and a pin aimed at `authelia.local.<domain>` reaches nothing.

    Non-vacuity: this asserts the two named services whose hostname does and does not differ
    from the service name, so the lookup silently returning its argument — an empty
    containers_list, a renamed key — fails here rather than passing on the fallback.
    """
    assert postflight.route_host("authelia") == "auth"
    assert postflight.route_host("jellyfin") == "jellyfin"


def test_an_unanswered_clusterip_falls_back_to_the_traefik_route(monkeypatch):
    """#1633: postflight runs on daniel-box only, so a pod on daniel-server had no asker.

    The ClusterIP answers only a caller on the pod's own node — each workload's NetworkPolicy
    admits pod selectors and no ipBlock for the node — which made §9.5 and §9.3 structurally
    SKIP rather than ever reporting. The route reaches either node.
    """
    seen = []

    def answer(url, header=None, timeout=postflight.TIMEOUT, resolve=None):
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
    daniel-server, so both 302 for this host — measured 2026-09-10. Letting that reach the
    tail arm read `HTTP 302 — prowlarr_api_key doesn't match`, sending someone to rotate a
    working credential: strictly worse than the SKIP it replaced.
    """
    respond(monkeypatch, 302)
    status, detail = postflight.check_arr_key("prowlarr")
    assert status == postflight.SKIP
    assert "forward-auth" in detail
    assert "doesn't match" not in detail


def test_a_workload_with_no_service_skips_not_fails(monkeypatch):
    def absent(name):
        raise postflight.Skip(f"{name} has no ClusterIP (does the Service exist?)")

    monkeypatch.setattr(postflight, "service_ip", absent)
    only_checks(
        monkeypatch, [("9.3", "sonarr", lambda: postflight.check_arr_key("sonarr"))]
    )
    assert postflight.main() == 0


def test_one_failure_exits_nonzero(monkeypatch):
    only_checks(monkeypatch, [("9.1", "x", lambda: (postflight.FAIL, "broken"))])
    assert postflight.main() == 1


def test_check_raising_does_not_abort_the_run(monkeypatch):
    """One check blowing up must not hide the checks after it."""

    def boom():
        raise ValueError("bad json")

    only_checks(
        monkeypatch, [("9.1", "x", boom), ("9.2", "y", lambda: (postflight.OK, "fine"))]
    )
    assert postflight.main() == 1


def test_get_parses_status_and_body(monkeypatch):
    class Result:
        returncode = 0
        stdout = '{"a": 1}\n200'
        stderr = ""

    stub_curl(monkeypatch, lambda *a, **kw: Result())
    assert postflight.get("http://x") == (200, '{"a": 1}')


def test_get_reports_curl_failure_as_status_zero(monkeypatch):
    class Result:
        returncode = 7
        stdout = ""
        stderr = "connection refused"

    stub_curl(monkeypatch, lambda *a, **kw: Result())
    assert postflight.get("http://x") == (0, "connection refused")


def test_credentials_never_reach_argv(monkeypatch):
    """The auth header goes in on stdin — a secret in argv would land in `ps`."""
    seen = {}

    class Result:
        returncode = 0
        stdout = "\n200"
        stderr = ""

    def fake_run(argv, input=None, **kw):
        seen["argv"] = argv
        seen["input"] = input
        return Result()

    stub_curl(monkeypatch, fake_run)
    postflight.get("http://x", 'header = "X-Api-Key: hunter2"\n')
    assert "hunter2" not in " ".join(seen["argv"])
    assert "hunter2" in seen["input"]
