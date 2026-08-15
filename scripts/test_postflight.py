#!/usr/bin/env python3
"""Tests for postflight.py — the ansible/README.md §9 verifier.

The point of the script is that each §9 step fails SILENTLY in production, so the
thing worth testing is that a bad response is reported as FAIL rather than passing
through. HTTP and Docker are injected out, so these are hermetic.

Run: uv run pytest scripts/test_postflight.py
"""

import importlib.util
import json
import os

import pytest


_MOD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "postflight.py")
_spec = importlib.util.spec_from_file_location("postflight", _MOD)
postflight = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(postflight)


@pytest.fixture(autouse=True)
def stub_host(monkeypatch):
    """Every container resolves, and every secret decrypts to a placeholder."""
    monkeypatch.setattr(postflight, "container_ip", lambda name: "10.0.0.1")
    monkeypatch.setattr(postflight, "secret", lambda name: (f"<{name}>", ""))
    # check_ha_token reaches HA via probe.ha_base(), which decrypts the domain from
    # SOPS — stub it so no test needs the age key (CI has none). Same for the cluster
    # prometheus route the Kuma checks query since the PG1 scrape port.
    monkeypatch.setattr(postflight.probe, "ha_base", lambda: "https://ha.test")
    monkeypatch.setattr(
        postflight.probe,
        "k8s_endpoint",
        lambda h: (f"https://{h}.test", f"{h}.test:443:10.0.0.240"),
    )


def respond(monkeypatch, status, body=""):
    monkeypatch.setattr(postflight, "get", lambda *a, **kw: (status, body))


def targets_body(*targets):
    return json.dumps({"data": {"activeTargets": list(targets)}})


def target(job, health="up", last_error=""):
    return {"labels": {"job": job}, "health": health, "lastError": last_error}


# --- §9.1 Uptime-Kuma admin --------------------------------------------------


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


# --- §9.2 Kuma API key -------------------------------------------------------


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


# --- §9.3 *arr / jellyfin keys -----------------------------------------------


def test_arr_key_mismatch_fails(monkeypatch):
    respond(monkeypatch, 401)
    status, detail = postflight.check_arr_key("sonarr")
    assert status == postflight.FAIL
    assert "sonarr_api_key" in detail


def test_arr_key_ok(monkeypatch):
    respond(monkeypatch, 200, "{}")
    assert postflight.check_arr_key("radarr")[0] == postflight.OK


def test_jellyfin_key_mismatch_fails(monkeypatch):
    respond(monkeypatch, 401)
    assert postflight.check_jellyfin_key()[0] == postflight.FAIL


# --- §9.4 HA tokens ----------------------------------------------------------


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


# --- §9.5 Authelia -----------------------------------------------------------


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


# --- runner ------------------------------------------------------------------


def test_missing_container_skips_not_fails(monkeypatch):
    def absent(name):
        raise postflight.Skip(f"{name} has no container IP (is it running?)")

    monkeypatch.setattr(postflight, "container_ip", absent)
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
