#!/usr/bin/env python3
"""Tests for postflight.py — the ansible/README.md §9 verifier.

The point of the script is that each §9 step fails SILENTLY in production, so the
thing worth testing is that a bad response is reported as FAIL rather than passing
through. HTTP and Docker are injected out, so these are hermetic.

Run: uv run pytest scripts/diagnostics/test_postflight.py
"""

import json

import pytest

import postflight


@pytest.fixture(autouse=True)
def stub_host(monkeypatch):
    """Every workload resolves, and every secret decrypts to a placeholder."""
    monkeypatch.setattr(postflight, "service_ip", lambda name: "10.0.0.1")
    monkeypatch.setattr(postflight, "secret", lambda name: (f"<{name}>", ""))
    # check_ha_token reaches HA via probe_core.ha_base(), which decrypts the domain from
    # SOPS — stub it so no test needs the age key (CI has none). Same for the cluster
    # prometheus route the Kuma checks query since the PG1 scrape port.
    monkeypatch.setattr(postflight.core, "ha_base", lambda: "https://ha.test")
    monkeypatch.setattr(
        postflight.core,
        "k8s_endpoint",
        lambda h: (f"https://{h}.test", f"{h}.test:443:10.0.0.240"),
    )


def respond(monkeypatch, status, body=""):
    monkeypatch.setattr(postflight, "get", lambda *a, **kw: (status, body))


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
        postflight.probe_health.core, "k8s_namespace", lambda: "homelab"
    )

    class Result:
        returncode, stdout, stderr = 0, "10.43.0.9\n", ""

    monkeypatch.setattr(
        postflight.probe_health.subprocess,
        "run",
        lambda argv, **kw: (seen.append(argv), Result())[1],
    )
    assert postflight.probe_health.resolve_service_ip("sonarr") == "10.43.0.9"
    assert "docker" not in seen[0]
    assert "service" in seen[0]


def test_jellyfin_key_mismatch_fails(monkeypatch):
    respond(monkeypatch, 401)
    assert postflight.check_jellyfin_key()[0] == postflight.FAIL


def test_ha_token_rejected_fails(monkeypatch):
    respond(monkeypatch, 401)
    assert postflight.check_ha_token("claude_ha_token")[0] == postflight.FAIL


def test_ha_token_missing_from_sops_fails(monkeypatch):
    monkeypatch.setattr(postflight, "secret", lambda name: ("", "not found"))
    respond(monkeypatch, 200)
    assert postflight.check_ha_token("homepage_ha_token") == (
        postflight.FAIL,
        "not found",
    )


def test_authelia_missing_oidc_material_fails(monkeypatch):
    respond(monkeypatch, 200, json.dumps({"status": "OK"}))
    monkeypatch.setattr(
        postflight,
        "secret",
        lambda name: ("", "") if name == "authelia_oidc_hmac_secret" else ("x", ""),
    )
    status, detail = postflight.check_authelia()
    assert status == postflight.FAIL
    assert "authelia_oidc_hmac_secret" in detail


def test_a_workload_with_no_service_skips_not_fails(monkeypatch):
    def absent(name):
        raise postflight.Skip(f"{name} has no ClusterIP (does the Service exist?)")

    monkeypatch.setattr(postflight, "service_ip", absent)
    monkeypatch.setattr(
        postflight,
        "CHECKS",
        [("9.3", "sonarr", lambda: postflight.check_arr_key("sonarr"))],
    )
    assert postflight.main() == 0


def test_one_failure_exits_nonzero(monkeypatch):
    monkeypatch.setattr(
        postflight, "CHECKS", [("9.1", "x", lambda: (postflight.FAIL, "broken"))]
    )
    assert postflight.main() == 1


def test_check_raising_does_not_abort_the_run(monkeypatch):
    """One check blowing up must not hide the checks after it."""

    def boom():
        raise ValueError("bad json")

    monkeypatch.setattr(
        postflight,
        "CHECKS",
        [("9.1", "x", boom), ("9.2", "y", lambda: (postflight.OK, "fine"))],
    )
    assert postflight.main() == 1


def test_get_parses_status_and_body(monkeypatch):
    class Result:
        returncode = 0
        stdout = '{"a": 1}\n200'
        stderr = ""

    monkeypatch.setattr(postflight.subprocess, "run", lambda *a, **kw: Result())
    assert postflight.get("http://x") == (200, '{"a": 1}')


def test_get_reports_curl_failure_as_status_zero(monkeypatch):
    class Result:
        returncode = 7
        stdout = ""
        stderr = "connection refused"

    monkeypatch.setattr(postflight.subprocess, "run", lambda *a, **kw: Result())
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

    monkeypatch.setattr(postflight.subprocess, "run", fake_run)
    postflight.get("http://x", 'header = "X-Api-Key: hunter2"\n')
    assert "hunter2" not in " ".join(seen["argv"])
    assert "hunter2" in seen["input"]
