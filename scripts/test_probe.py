#!/usr/bin/env python3
"""Tests for probe.py — the consolidated read-only homelab diagnostics wrapper.

probe.py replaces a pile of one-off `curl http://<container-ip>:<port>/...` and
`openssl s_client` commands that each became a dead, never-reused allow-list
entry. The whole point is one allow-listed surface, so the routing + URL building
must be correct. Network/Docker calls are injected out via a fake resolver, so
these tests are hermetic.

Run: uv run pytest scripts/test_probe.py
"""

import importlib.util
import os
import re
from datetime import datetime, timezone

import pytest


_MOD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe.py")
_spec = importlib.util.spec_from_file_location("probe", _MOD)
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)

# Fake resolver: maps container name -> a recognizable IP. A wrong container name
# raises KeyError, so a misrouted subcommand fails loudly.
IPS = {"prometheus": "10.0.0.1", "loki": "10.0.0.2", "scrutiny": "10.0.0.3"}
fake_resolve = IPS.__getitem__


def test_prom_query_url_encodes_promql():
    url = probe.prom_query_url("https://prom.example", "up == 0")
    assert url == "https://prom.example/api/v1/query?query=up+%3D%3D+0"


def test_prom_targets_url():
    assert (
        probe.prom_targets_url("https://prom.example")
        == "https://prom.example/api/v1/targets"
    )


def test_loki_labels_url():
    assert (
        probe.loki_labels_url("https://loki.example")
        == "https://loki.example/loki/api/v1/labels"
    )


def test_loki_query_url_encodes_logql_and_limit():
    url = probe.loki_query_url("https://loki.example", '{job="x"}', 50)
    assert (
        url
        == "https://loki.example/loki/api/v1/query_range?query=%7Bjob%3D%22x%22%7D&limit=50"
    )


def test_scrutiny_url():
    assert (
        probe.scrutiny_url("https://scrutiny.example")
        == "https://scrutiny.example/api/summary"
    )


def test_pi_url():
    assert probe.pi_url("fs") == "http://daniel-pi.lan:61208/api/4/fs"


def test_pi_ip_reads_real_inventory():
    # hosts.ini is plaintext, not a secret — same class of dead-path bug as
    # test_verify_automations_path_exists below: a wrong path or regex would only ever
    # be caught by opening the real file.
    ip = probe.pi_ip()
    assert re.match(r"^\d+\.\d+\.\d+\.\d+$", ip), ip


def test_pi_resolve_pins_the_lan_ip(monkeypatch):
    monkeypatch.setattr(probe, "pi_ip", lambda: "10.0.0.139")
    assert probe.pi_resolve() == "daniel-pi.lan:61208:10.0.0.139"


def test_curl_argv():
    assert probe.curl_argv("http://x") == [
        "curl",
        "-sS",
        "--max-time",
        "10",
        "http://x",
    ]


def test_inspect_ip_argv_targets_the_container():
    argv = probe.inspect_ip_argv("loki")
    assert argv[:3] == ["docker", "inspect", "-f"]
    assert argv[-1] == "loki"
    assert ".IPAddress" in argv[3]


def test_parse_ip_takes_first_nonempty_token():
    assert probe.parse_ip("172.19.0.12 172.18.0.5 \n") == "172.19.0.12"


def test_parse_ip_returns_none_when_no_ip():
    assert probe.parse_ip("   \n") is None


def test_k8s_service_ip_argv_targets_the_service():
    argv = probe.k8s_service_ip_argv("sonarr", "homelab")
    assert argv[:2] == ["k3s", "kubectl"]
    assert argv[-1] == "jsonpath={.spec.clusterIP}"
    assert "sonarr" in argv
    assert "homelab" in argv


def test_plan_metric_uses_cluster_prometheus_route():
    # The Docker prometheus (resolve_ip target) retired 2026-08-14 with the drain.
    stages = probe.plan(["metric", "up == 0"], fake_resolve, fake_k8s_endpoint)
    assert stages == [
        probe.curl_argv(
            "https://prometheus.example/api/v1/query?query=up+%3D%3D+0",
            resolve="prometheus.example:443:10.0.0.240",
        )
    ]


def test_plan_targets_uses_cluster_prometheus_route():
    stages = probe.plan(["targets"], fake_resolve, fake_k8s_endpoint)
    assert stages == [
        probe.curl_argv(
            "https://prometheus.example/api/v1/targets",
            resolve="prometheus.example:443:10.0.0.240",
        )
    ]


def fake_k8s_endpoint(hostname):
    # The (base, --resolve pin) pair the live k8s_endpoint() derives from SOPS +
    # inventory — faked so plan() stays testable without either.
    return f"https://{hostname}.example", f"{hostname}.example:443:10.0.0.240"


def test_plan_loki_labels_uses_cluster_endpoint_with_vip_pin():
    stages = probe.plan(["loki-labels"], fake_resolve, fake_k8s_endpoint)
    assert stages == [
        probe.curl_argv(
            "https://loki-homelab.example/loki/api/v1/labels",
            resolve="loki-homelab.example:443:10.0.0.240",
        )
    ]


def test_plan_loki_query_with_limit():
    stages = probe.plan(
        ["loki-query", '{job="x"}', "--limit", "50"], fake_resolve, fake_k8s_endpoint
    )
    assert stages == [
        probe.curl_argv(
            probe.loki_query_url("https://loki-homelab.example", '{job="x"}', 50),
            resolve="loki-homelab.example:443:10.0.0.240",
        )
    ]


def test_plan_scrutiny_uses_cluster_endpoint_with_vip_pin():
    stages = probe.plan(["scrutiny"], fake_resolve, fake_k8s_endpoint)
    assert stages == [
        probe.curl_argv(
            "https://scrutiny.example/api/summary",
            resolve="scrutiny.example:443:10.0.0.240",
        )
    ]


def test_plan_pi_does_not_resolve_docker():
    # Pi glances is reached by hostname, so the container resolver must NOT be consulted.
    def boom(_):
        raise AssertionError("pi must not resolve a container IP")

    stages = probe.plan(
        ["pi", "fs"], boom, pi_resolve=lambda: "daniel-pi.lan:61208:10.0.0.139"
    )
    assert stages == [
        probe.curl_argv(
            "http://daniel-pi.lan:61208/api/4/fs",
            resolve="daniel-pi.lan:61208:10.0.0.139",
        )
    ]


def test_plan_pi_resolves_to_a_reachable_pin_not_dns():
    # Regression guard: this host's resolver has no answer for daniel-pi.lan (a
    # Pi-hole-only LAN name), so plan() must always carry a --resolve pin here — a bare
    # curl_argv() with no pin is the pre-fix shape that died `Could not resolve host`.
    stages = probe.plan(
        ["pi", "fs"], None, pi_resolve=lambda: "daniel-pi.lan:61208:10.0.0.139"
    )
    assert "--resolve" in stages[0]
    assert "daniel-pi.lan:61208:10.0.0.139" in stages[0]


def test_plan_cert_defaults_port_and_sni_to_host():
    stages = probe.plan(["cert", "homepage.daniel-hunter.com"], fake_resolve)
    assert stages == probe.cert_stages(
        "homepage.daniel-hunter.com", 443, "homepage.daniel-hunter.com"
    )


def test_plan_cert_explicit_port_and_sni():
    stages = probe.plan(
        ["cert", "10.0.0.161:443", "--sni", "homepage.daniel-hunter.com"], fake_resolve
    )
    assert stages == probe.cert_stages("10.0.0.161", 443, "homepage.daniel-hunter.com")


def test_cert_stages_is_a_two_stage_pipeline():
    s1, s2 = probe.cert_stages("h", 443, "h")
    assert s1[:2] == ["openssl", "s_client"]
    assert "h:443" in s1
    assert s2[:2] == ["openssl", "x509"]


def _inspect(state, restarts=0):
    return [{"State": state, "RestartCount": restarts}]


def test_inspect_argv():
    assert probe.inspect_argv("jellyfin") == ["docker", "inspect", "jellyfin"]


def test_health_running_and_healthy_exits_zero():
    data = _inspect(
        {
            "Status": "running",
            "Health": {
                "Status": "healthy",
                "FailingStreak": 0,
                "Log": [{"Output": "ok\n"}],
            },
        }
    )
    text, code = probe.format_health(data, "jellyfin")
    assert code == 0
    assert "healthy" in text and "running" in text


def test_health_unhealthy_exits_one_and_shows_streak_and_last_log():
    data = _inspect(
        {
            "Status": "running",
            "Health": {
                "Status": "unhealthy",
                "FailingStreak": 3,
                "Log": [{"Output": "connection refused\n"}],
            },
        }
    )
    text, code = probe.format_health(data, "qbittorrent")
    assert code == 1
    assert "unhealthy" in text and "3" in text and "connection refused" in text


def test_health_no_healthcheck_running_exits_zero():
    text, code = probe.format_health(_inspect({"Status": "running"}), "valheim")
    assert code == 0
    assert "no healthcheck" in text


def test_health_exited_exits_one():
    text, code = probe.format_health(_inspect({"Status": "exited"}), "valheim")
    assert code == 1
    assert "exited" in text


def test_health_not_found_exits_one():
    text, code = probe.format_health([], "nope")
    assert code == 1
    assert "not found" in text


#
# `health` ran `docker inspect` unconditionally until 2026-08-16 and had been dead on both
# cluster nodes since the 2026-08-14 Docker retirement — neither has the binary, so it raised
# FileNotFoundError. Every case below is a way the k8s replacement could report healthy when it
# is not, which is the only direction that matters for a post-deploy gate.

_NOW = datetime(2026, 8, 16, 12, 0, 0, tzinfo=timezone.utc)


def _deploy(generation=1, observed=1, replicas=1, updated=1, ready=1, available=1):
    return {
        "metadata": {"generation": generation},
        "spec": {"replicas": replicas},
        "status": {
            "observedGeneration": observed,
            "updatedReplicas": updated,
            "readyReplicas": ready,
            "availableReplicas": available,
        },
    }


def _pods(*containers):
    """containers: (name, restart_count, finished_at_or_None)."""
    return {
        "items": [
            {
                "metadata": {"name": "svc-abc"},
                "status": {
                    "containerStatuses": [
                        {
                            "name": name,
                            "restartCount": count,
                            "lastState": (
                                {"terminated": {"finishedAt": finished}}
                                if finished
                                else {}
                            ),
                        }
                        for name, count, finished in containers
                    ]
                },
            }
        ]
    }


def test_k8s_health_rolled_out_and_quiet_exits_zero():
    text, code = probe.format_k8s_health(
        _deploy(), _pods(("app", 0, None)), "freshrss", _NOW
    )
    assert code == 0
    assert "1/1 ready" in text


def test_k8s_health_missing_deployment_exits_one():
    text, code = probe.format_k8s_health(None, None, "nope", _NOW)
    assert code == 1
    assert "no Deployment" in text


def test_k8s_health_stale_generation_exits_one():
    """The controller has not observed the spec change yet, so the OLD pod is what is ready."""
    text, code = probe.format_k8s_health(
        _deploy(generation=5, observed=4), _pods(("app", 0, None)), "freshrss", _NOW
    )
    assert code == 1
    assert "not observed yet" in text


def test_k8s_health_incomplete_rollout_exits_one():
    text, code = probe.format_k8s_health(
        _deploy(replicas=2, updated=1, ready=1, available=1),
        _pods(("app", 0, None)),
        "freshrss",
        _NOW,
    )
    assert code == 1
    assert "rollout incomplete" in text


def test_k8s_health_recent_restart_exits_one_despite_being_ready():
    """The kube-state-metrics failure of 2026-08-07: a bad liveness probe passes READINESS,
    flips the Deployment to Available, and only then starts getting killed. Every
    readiness-derived field reads healthy while the pod crashloops."""
    just_now = "2026-08-16T11:59:30Z"
    text, code = probe.format_k8s_health(
        _deploy(), _pods(("app", 3, just_now)), "kube-state-metrics", _NOW
    )
    assert code == 1
    assert "RECENT RESTART" in text and "30s ago" in text


def test_k8s_health_old_restart_does_not_fail():
    """A pod that restarted last week and has been up since is healthy — restartCount alone
    would fail it forever."""
    last_week = "2026-08-09T12:00:00Z"
    text, code = probe.format_k8s_health(
        _deploy(), _pods(("app", 3, last_week)), "freshrss", _NOW
    )
    assert code == 0
    assert "restarts=3" in text


def test_k8s_health_unparseable_restart_timestamp_does_not_fail_open():
    """An unreadable finishedAt must count as RECENT, not as 'long ago'.

    Treating unknown as old is the one direction a gate must never fail. Reachable whenever
    kubectl's timestamp format shifts — fractional seconds, for instance, parse as None.
    """
    assert probe._seconds_since("not-a-timestamp", _NOW) is None
    assert probe._seconds_since(None, _NOW) is None

    text, code = probe.format_k8s_health(
        _deploy(), _pods(("app", 1, "2026-08-16T11:59:30.123456Z")), "freshrss", _NOW
    )
    assert code == 1
    assert "unreadable time" in text


def test_k8s_health_restart_with_no_laststate_fails_closed():
    """restartCount > 0 with no terminated state is still an unexplained restart."""
    text, code = probe.format_k8s_health(
        _deploy(), _pods(("app", 2, None)), "freshrss", _NOW
    )
    assert code == 1
    assert "unreadable time" in text


def test_k8s_health_checks_every_container_in_the_pod():
    """A sidecar crashlooping while the main container is fine still fails the gate."""
    just_now = "2026-08-16T11:59:00Z"
    text, code = probe.format_k8s_health(
        _deploy(), _pods(("app", 0, None), ("sidecar", 9, just_now)), "n8n", _NOW
    )
    assert code == 1
    assert "sidecar" in text


def _daemonset(generation=1, observed=1, desired=2, updated=2, ready=2, available=2):
    return {
        "kind": "DaemonSet",
        "metadata": {"generation": generation},
        "status": {
            "observedGeneration": observed,
            "desiredNumberScheduled": desired,
            "updatedNumberScheduled": updated,
            "numberReady": ready,
            "numberAvailable": available,
        },
    }


def test_k8s_health_reads_a_daemonset():
    """Six workloads here are DaemonSets — promtail, node-exporter, the crowdsec node agent.
    They carry the same four numbers under different status field names."""
    text, code = probe.format_k8s_health(
        _daemonset(), _pods(("app", 0, None)), "promtail", _NOW
    )
    assert code == 0
    assert "2/2 ready" in text


def test_k8s_health_daemonset_missing_a_node_exits_one():
    """Scheduled on 2 nodes, ready on 1 — a Deployment's readyReplicas would read 0 here, so
    the field mapping has to be per-kind rather than a shared default."""
    text, code = probe.format_k8s_health(
        _daemonset(ready=1, available=1), _pods(("app", 0, None)), "promtail", _NOW
    )
    assert code == 1
    assert "rollout incomplete" in text


def test_k8s_health_argv_can_ask_for_a_daemonset():
    assert "daemonset" in probe.k8s_deploy_argv("promtail", "homelab", kind="daemonset")


def test_k8s_health_argv_targets_the_named_namespace():
    assert probe.k8s_deploy_argv("freshrss", "homelab")[:4] == [
        "k3s",
        "kubectl",
        "-n",
        "homelab",
    ]
    assert "app=freshrss" in probe.k8s_pods_argv("freshrss", "homelab")


def test_ha_base_builds_on_ha_host(monkeypatch):
    # ha_host() decrypts the domain from SOPS; stub it — CI has no age key.
    monkeypatch.setattr(probe, "sops_extract", lambda key: "example.test")
    assert probe.ha_host() == "home-assistant.local.example.test"
    assert probe.ha_base() == "https://home-assistant.local.example.test"


def test_ha_resolve_pins_vip(monkeypatch):
    # Since the bridge teardown (slice-7 BT4) host-shell DNS for the .local name rides the
    # Cloudflare wildcard, so every HA call pins the name to the ingress VIP.
    monkeypatch.setattr(probe, "sops_extract", lambda key: "example.test")
    monkeypatch.setattr(probe, "metallb_vip", lambda: "10.0.0.240")
    assert probe.ha_resolve() == "home-assistant.local.example.test:443:10.0.0.240"


def test_ha_curl_argv_resolve_precedes_url():
    argv = probe.ha_curl_argv("https://h/api/states/x", resolve="h:443:10.0.0.240")
    assert argv[-1] == "https://h/api/states/x"
    assert argv[argv.index("--resolve") + 1] == "h:443:10.0.0.240"


def test_ha_state_url():
    # The base is the bridge URL since slice-5 B3 (HA in the cluster — no container to inspect).
    assert (
        probe.ha_state_url("https://ha.example", "fan.tower_fan")
        == "https://ha.example/api/states/fan.tower_fan"
    )


def test_ha_get_url_bare_path():
    assert (
        probe.ha_get_url("https://ha.example", "error_log")
        == "https://ha.example/api/error_log"
    )


def test_ha_get_url_normalizes_leading_slash_and_api_prefix():
    # A user may type any of these; all mean the same endpoint.
    for path in ("error_log", "/error_log", "api/error_log", "/api/error_log"):
        assert probe.ha_get_url("https://h", path) == "https://h/api/error_log"


def _auto(
    entity_id, _id, friendly, state="on", last_triggered="2026-06-20T12:00:00+00:00"
):
    return {
        "entity_id": entity_id,
        "state": state,
        "attributes": {
            "id": _id,
            "friendly_name": friendly,
            "last_triggered": last_triggered,
        },
    }


_HA_STATES = [
    {"entity_id": "fan.tower_fan", "state": "on", "attributes": {}},
    _auto("automation.bedroom_presence_on", "presence_1", "Bedroom Presence On"),
    # The CLAUDE.md trap: alias-slug != id. The id is bedroom_fan_temperature,
    # but the entity_id (derived from the alias) is ..._control.
    _auto(
        "automation.bedroom_fan_temperature_control",
        "bedroom_fan_temperature",
        "Bedroom Fan Temperature Control",
    ),
]


def test_match_automation_by_entity_slug():
    m = probe.match_automation(_HA_STATES, "bedroom_presence_on")
    assert m["entity_id"] == "automation.bedroom_presence_on"


def test_match_automation_by_id_when_alias_differs():
    # Querying the id finds the entity even though its slug differs — the whole point.
    m = probe.match_automation(_HA_STATES, "bedroom_fan_temperature")
    assert m["entity_id"] == "automation.bedroom_fan_temperature_control"


def test_match_automation_by_friendly_name_slug():
    m = probe.match_automation(_HA_STATES, "bedroom_fan_temperature_control")
    assert m["attributes"]["id"] == "bedroom_fan_temperature"


def test_match_automation_accepts_full_entity_id():
    m = probe.match_automation(_HA_STATES, "automation.bedroom_presence_on")
    assert m["attributes"]["id"] == "presence_1"


def test_match_automation_none_for_unknown():
    assert probe.match_automation(_HA_STATES, "does_not_exist") is None


def test_match_automation_ignores_non_automation_domain():
    # "tower_fan" is a fan, not an automation — must not match.
    assert probe.match_automation(_HA_STATES, "tower_fan") is None


def test_ha_curl_argv_reads_header_from_stdin_config():
    argv = probe.ha_curl_argv("http://h:8123/api/states/x")
    assert "--config" in argv and "-" in argv
    assert argv[-1] == "http://h:8123/api/states/x"


def test_ha_curl_argv_carries_no_token():
    # Regression guard: no element of argv may carry the bearer token (ps/history).
    argv = probe.ha_curl_argv("http://h:8123/api/states/x")
    assert not any("Bearer" in a or "Authorization" in a for a in argv)


def test_ha_curl_config_has_bearer_header():
    cfg = probe.ha_curl_config("SECRET_TOKEN")
    assert 'header = "Authorization: Bearer SECRET_TOKEN"' in cfg


def test_format_ha_state_shows_entity_state_and_name():
    obj = {
        "entity_id": "fan.tower_fan",
        "state": "on",
        "attributes": {"friendly_name": "Tower Fan"},
        "last_changed": "2026-06-20T12:00:00+00:00",
        "last_updated": "2026-06-20T12:00:00+00:00",
    }
    out = probe.format_ha_state(obj)
    assert "fan.tower_fan" in out and "on" in out and "Tower Fan" in out
    assert "last_changed=2026-06-20T12:00:00+00:00" in out


def test_format_ha_automation_includes_id_and_last_triggered():
    obj = _auto("automation.bedroom_presence_on", "presence_1", "Bedroom Presence On")
    out = probe.format_ha_automation(obj)
    assert "automation.bedroom_presence_on" in out
    assert "presence_1" in out
    assert "last_triggered=2026-06-20T12:00:00+00:00" in out
    assert "Bedroom Presence On" in out


def test_ws_encode_is_masked_client_text_frame():
    frame = probe._ws_encode("hello")
    assert frame[0] == 0x81  # FIN + text opcode
    assert frame[1] == 0x80 | 5  # mask bit + 5-byte length
    mask, body = frame[2:6], frame[6:]
    assert bytes(b ^ mask[i % 4] for i, b in enumerate(body)) == b"hello"


def test_ws_encode_extended_length_126():
    payload = "x" * 200
    frame = probe._ws_encode(payload)
    assert frame[1] == 0x80 | 126  # 126 sentinel -> 16-bit length follows
    assert frame[2:4] == (200).to_bytes(2, "big")


def test_ws_read_frame_decodes_unmasked_text():
    payload = b'{"type":"auth_ok"}'
    raw = bytes([0x81, len(payload)]) + payload
    pos = [0]

    def recv_exact(n):
        chunk = raw[pos[0] : pos[0] + n]
        pos[0] += n
        return chunk

    assert probe._ws_read_frame(recv_exact) == '{"type":"auth_ok"}'


def test_ws_read_frame_decodes_extended_length():
    payload = b"y" * 300
    raw = bytes([0x81, 126]) + (300).to_bytes(2, "big") + payload
    pos = [0]

    def recv_exact(n):
        chunk = raw[pos[0] : pos[0] + n]
        pos[0] += n
        return chunk

    assert probe._ws_read_frame(recv_exact) == "y" * 300


_TRACE_BLOCKED = {
    # Real HA trace/get shape (confirmed against live daniel-server 2026-06-22):
    # `trigger` is a plain string description, NOT a dict.
    "trigger": "state of binary_sensor.aqara_fp300_presence",
    "trace": {
        "trigger/0": [{"path": "trigger/0", "result": {}}],
        "condition/0": [{"path": "condition/0", "result": {"result": False}}],
    },
    "error": None,
}


def test_format_trace_marks_failed_condition():
    out = probe.format_trace(_TRACE_BLOCKED)
    assert "binary_sensor.aqara_fp300_presence" in out
    assert "condition/0" in out
    assert "FAIL" in out


def test_format_trace_none_is_explained():
    assert "no stored trace" in probe.format_trace(None)


def test_format_trace_reports_error():
    out = probe.format_trace({"trigger": {}, "trace": {}, "error": "boom"})
    assert "boom" in out


def test_expected_automation_ids_matches_top_level_only():
    from probe import expected_automation_ids

    text = (
        "- id: bedroom_presence_on\n"
        "  alias: Presence on\n"
        "  trigger:\n"
        "    - id: co2_bad\n"  # indented trigger id must NOT be captured
        "      platform: state\n"
        "- id: ha_heartbeat\n"
        "  alias: HA heartbeat\n"
    )
    assert expected_automation_ids(text) == {"bedroom_presence_on", "ha_heartbeat"}


def test_automation_load_errors_flags_missing_and_unavailable():
    from probe import automation_load_errors

    expected = {"a_loaded", "b_missing", "c_unavailable", "d_disabled"}
    live = [
        {"entity_id": "automation.a", "state": "on", "attributes": {"id": "a_loaded"}},
        {
            "entity_id": "automation.c",
            "state": "unavailable",
            "attributes": {"id": "c_unavailable"},
        },
        {
            "entity_id": "automation.d",
            "state": "off",
            "attributes": {"id": "d_disabled"},
        },
        {
            "entity_id": "automation.x",
            "state": "on",
            "attributes": {"id": "cruft_not_in_file"},
        },
    ]
    errs = automation_load_errors(expected, live)
    assert errs == [
        "automation b_missing is defined in automations.yaml but did not load",
        "automation c_unavailable loaded but is unavailable (config error at load)",
    ]


def test_automation_load_errors_clean_when_all_loaded():
    from probe import automation_load_errors

    expected = {"a", "b"}
    live = [
        {"entity_id": "automation.a", "state": "on", "attributes": {"id": "a"}},
        {"entity_id": "automation.b", "state": "off", "attributes": {"id": "b"}},
    ]
    assert automation_load_errors(expected, live) == []


def test_automation_load_errors_tolerates_missing_attributes():
    # A live entity with attributes null or absent must be skipped, not raise — exercises the
    # `(a.get("attributes") or {})` guard. (No expected id matches them, so they're ignored.)
    from probe import automation_load_errors

    expected = {"a"}
    live = [
        {"entity_id": "automation.weird", "state": "on", "attributes": None},
        {"entity_id": "automation.nope", "state": "on"},  # no attributes key
        {"entity_id": "automation.a", "state": "on", "attributes": {"id": "a"}},
    ]
    assert automation_load_errors(expected, live) == []


def test_verify_automations_subcommand_parses():
    from probe import _build_parser

    ns = _build_parser().parse_args(["ha", "verify-automations"])
    assert ns.cmd == "ha" and ns.ha_cmd == "verify-automations"


def test_snapshot_entity_ids_parses_list_items():
    from probe import snapshot_entity_ids

    text = (
        "# generated\n"
        "entities:\n"
        "  - sensor.pixel_watch_3_do_not_disturb_sensor\n"
        "  - binary_sensor.aqara_fp300_presence\n"
        "  - not_an_entity\n"
    )
    assert snapshot_entity_ids(text) == {
        "sensor.pixel_watch_3_do_not_disturb_sensor",
        "binary_sensor.aqara_fp300_presence",
    }


def test_vanished_snapshot_entities_reports_only_absent_ids():
    from probe import vanished_snapshot_entities

    snapshot = {"sensor.a", "sensor.gone", "sensor.b"}
    live = ["sensor.a", "sensor.b", "sensor.extra_not_in_snapshot"]
    # Only snapshot ids missing live are reported; live-only ids are not the gate's business.
    assert vanished_snapshot_entities(snapshot, live) == ["sensor.gone"]
    assert vanished_snapshot_entities(snapshot, list(snapshot)) == []


def test_verify_entities_snapshot_path_exists():
    """Same pinning as the automations gate — the snapshot must be readable and parseable."""
    from probe import EXTERNAL_ENTITIES_YAML, snapshot_entity_ids

    assert os.path.isfile(EXTERNAL_ENTITIES_YAML), f"{EXTERNAL_ENTITIES_YAML} missing"
    with open(EXTERNAL_ENTITIES_YAML, encoding="utf-8") as f:
        assert snapshot_entity_ids(f.read()), "no entity ids parsed from the snapshot"


def test_verify_automations_path_exists():
    """The gate's source file must actually be readable.

    This assertion is the whole point: AUTOMATIONS_YAML pointed at the pre-k3s
    `roles/containers/home-assistant/` path from the slice-5 cutover until 2026-08-16, so
    `probe.py ha verify-automations` raised FileNotFoundError every time it ran. The parse
    test above passed throughout, because it never opens the file. Reading it here means a
    future move of the role breaks a test instead of the post-deploy gate.
    """
    from probe import AUTOMATIONS_YAML, expected_automation_ids

    assert os.path.isfile(AUTOMATIONS_YAML), f"{AUTOMATIONS_YAML} is not readable"
    with open(AUTOMATIONS_YAML, encoding="utf-8") as f:
        ids = expected_automation_ids(f.read())
    assert ids, "no automation ids parsed from the git-managed source"


# These replace the `probe.py metric … | python3 -c "…reshape JSON…"` one-liners
# that kept prompting: the reshape now lives in the allow-listed script instead.


def test_format_metric_vector_prints_labels_and_value_per_series():
    data = {
        "data": {
            "resultType": "vector",
            "result": [
                {
                    "metric": {"__name__": "monitor_status", "monitor_name": "Pi"},
                    "value": [1720000000, "1"],
                },
                {
                    "metric": {"__name__": "monitor_status", "monitor_name": "Loki"},
                    "value": [1720000000, "0"],
                },
            ],
        }
    }
    out = probe.format_metric(data)
    assert "monitor_name=Pi = 1" in out
    assert "monitor_name=Loki = 0" in out
    # __name__ is dropped — it's the metric name, redundant here.
    assert "__name__" not in out


def test_format_metric_single_unlabeled_series_prints_bare_value():
    # e.g. predict_linear(...)/1e9 strips all labels -> just the scalar.
    data = {
        "data": {
            "resultType": "vector",
            "result": [{"metric": {}, "value": [0, "6.47"]}],
        }
    }
    assert probe.format_metric(data) == "6.47"


def test_format_metric_matrix_uses_latest_point():
    data = {
        "data": {
            "resultType": "matrix",
            "result": [
                {
                    "metric": {"mountpoint": "/"},
                    "values": [[1, "10"], [2, "12"], [3, "15"]],
                }
            ],
        }
    }
    assert probe.format_metric(data) == "mountpoint=/ = 15"


def test_format_metric_scalar_result_prints_value():
    data = {"data": {"resultType": "scalar", "result": [1720000000, "42"]}}
    assert probe.format_metric(data) == "42"


def test_format_metric_empty_is_no_data():
    assert (
        probe.format_metric({"data": {"resultType": "vector", "result": []}})
        == "no data"
    )


def _monitor_series(name, status):
    return {"metric": {"monitor_name": name}, "value": [1720000000, status]}


def test_format_monitor_status_all_up():
    data = {
        "data": {
            "result": [_monitor_series("sonarr", "1"), _monitor_series("radarr", "1")]
        }
    }
    text, code = probe.format_monitor_status(data)
    assert text == "2/2 monitors up"
    assert code == 0


def test_format_monitor_status_lists_down_monitors_and_fails():
    data = {
        "data": {
            "result": [
                _monitor_series("sonarr", "1"),
                _monitor_series("terraria (game port)", "0"),
            ]
        }
    }
    text, code = probe.format_monitor_status(data)
    assert text == "1/2 monitors up\n  terraria (game port): DOWN"
    assert code == 1


def test_format_monitor_status_labels_pending_and_maintenance_as_not_up():
    data = {
        "data": {
            "result": [_monitor_series("a", "2"), _monitor_series("b", "3")],
        }
    }
    text, code = probe.format_monitor_status(data)
    assert "a: PENDING" in text
    assert "b: MAINTENANCE" in text
    assert code == 1


def test_format_monitor_status_empty_result_fails():
    text, code = probe.format_monitor_status({"data": {"result": []}})
    assert code == 1
    assert "no monitor_status series" in text


def test_monitors_subcommand_parses():
    p = probe._build_parser()
    ns = p.parse_args(["monitors"])
    assert ns.cmd == "monitors"


def test_format_loki_prints_lines_oldest_first_across_streams():
    data = {
        "data": {
            "resultType": "streams",
            "result": [
                {
                    "stream": {"container": "traefik"},
                    "values": [["30", "newest"], ["10", "oldest"]],
                },
                {"stream": {"container": "traefik"}, "values": [["20", "middle"]]},
            ],
        }
    }
    # Sorted by nanosecond timestamp so the newest line sits nearest the prompt.
    assert probe.format_loki(data) == "oldest\nmiddle\nnewest"


def test_format_loki_empty_is_no_logs():
    assert probe.format_loki({"data": {"result": []}}) == "no logs"


def test_metric_defaults_to_formatted_with_json_escape_hatch():
    p = probe._build_parser()
    assert p.parse_args(["metric", "up"]).json is False
    assert p.parse_args(["metric", "up", "--json"]).json is True


def test_loki_query_defaults_to_formatted_with_json_escape_hatch():
    p = probe._build_parser()
    assert p.parse_args(["loki-query", '{job="x"}']).json is False
    assert p.parse_args(["loki-query", '{job="x"}', "--json"]).json is True


# Replaces `docker exec <arr> curl -H "X-Api-Key: <hex>" …/api/… | python3`,
# which both prompted AND leaked the key into argv / shell history / the log.


def test_arr_url_sonarr_defaults_to_api_v3_and_port_8989():
    assert (
        probe.arr_url("10.0.0.5", "sonarr", "health")
        == "http://10.0.0.5:8989/api/v3/health"
    )


def test_arr_url_radarr_port_7878_api_v3():
    assert (
        probe.arr_url("10.0.0.6", "radarr", "queue")
        == "http://10.0.0.6:7878/api/v3/queue"
    )


def test_arr_url_prowlarr_port_9696_api_v1():
    assert (
        probe.arr_url("10.0.0.7", "prowlarr", "indexerstatus")
        == "http://10.0.0.7:9696/api/v1/indexerstatus"
    )


def test_arr_url_normalizes_leading_slash_api_and_version_prefix():
    # bare, /-prefixed, api/-prefixed, and version-prefixed all mean the same endpoint.
    for path in ("health", "/health", "api/v3/health", "v3/health", "/api/v3/health"):
        assert probe.arr_url("h", "sonarr", path) == "http://h:8989/api/v3/health"


def test_arr_url_keeps_multi_segment_path():
    assert (
        probe.arr_url("h", "prowlarr", "indexer/testall")
        == "http://h:9696/api/v1/indexer/testall"
    )


def test_arr_curl_config_uses_x_api_key_header():
    assert 'header = "X-Api-Key: SECRET_KEY"' in probe.arr_curl_config("SECRET_KEY")


def test_arr_curl_config_is_not_bearer():
    cfg = probe.arr_curl_config("SECRET_KEY")
    assert "Bearer" not in cfg and "Authorization" not in cfg


def test_arr_request_never_puts_key_in_argv():
    # Regression guard mirroring the ha token: the key travels via stdin --config, never argv.
    argv = probe.ha_curl_argv(probe.arr_url("h", "sonarr", "health"))
    assert "--config" in argv
    assert not any("Api-Key" in a or "SECRET" in a for a in argv)


def test_resolve_arr_ip_uses_kubectl_not_docker(monkeypatch):
    # Regression guard for the dead command: sonarr/radarr/prowlarr have run as k8s
    # Deployments since 2026-08-07 and have no Docker container to `docker inspect` an IP
    # from — resolve_arr_ip must reach the app's ClusterIP via kubectl instead.
    monkeypatch.setattr(probe, "k8s_namespace", lambda: "homelab")

    class FakeResult:
        returncode = 0
        stdout = "10.43.114.186"
        stderr = ""

    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return FakeResult()

    monkeypatch.setattr(probe.subprocess, "run", fake_run)
    assert probe.resolve_arr_ip("sonarr") == "10.43.114.186"
    assert calls == [probe.k8s_service_ip_argv("sonarr", "homelab")]
    assert "docker" not in calls[0]


def test_resolve_arr_ip_raises_on_kubectl_failure(monkeypatch):
    monkeypatch.setattr(probe, "k8s_namespace", lambda: "homelab")

    class FakeResult:
        returncode = 1
        stdout = ""
        stderr = 'services "sonarr" not found'

    monkeypatch.setattr(probe.subprocess, "run", lambda argv, **kwargs: FakeResult())
    try:
        probe.resolve_arr_ip("sonarr")
        assert False, "expected SystemExit"
    except SystemExit as e:
        assert "sonarr" in str(e)


def test_resolve_arr_ip_raises_on_empty_cluster_ip(monkeypatch):
    monkeypatch.setattr(probe, "k8s_namespace", lambda: "homelab")

    class FakeResult:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(probe.subprocess, "run", lambda argv, **kwargs: FakeResult())
    try:
        probe.resolve_arr_ip("sonarr")
        assert False, "expected SystemExit"
    except SystemExit as e:
        assert "ClusterIP" in str(e)


def test_arr_subcommand_parses_app_path_and_json_flag():
    p = probe._build_parser()
    ns = p.parse_args(["arr", "sonarr", "health"])
    assert (
        ns.cmd == "arr"
        and ns.app == "sonarr"
        and ns.path == "health"
        and ns.json is False
    )
    assert p.parse_args(["arr", "prowlarr", "indexerstatus", "--json"]).json is True


def test_arr_subcommand_rejects_unknown_app():
    import pytest

    with pytest.raises(SystemExit):
        probe._build_parser().parse_args(["arr", "lidarr", "health"])


def test_loki_query_url_with_range_adds_start_end_direction():
    url = probe.loki_query_url(
        "10.0.0.2", '{job="x"}', 5000, start=1000, end=2000, direction="forward"
    )
    assert "start=1000" in url and "end=2000" in url and "direction=forward" in url


def test_rows_from_loki_flattens_and_sorts_streams():
    data = {
        "data": {
            "result": [
                {"values": [["20", "b"], ["10", "a"]]},
                {"values": [["30", "c"]]},
            ]
        }
    }
    assert probe._rows_from_loki(data) == [(10, "a"), (20, "b"), (30, "c")]


def test_rows_from_loki_handles_empty_and_missing_keys():
    assert probe._rows_from_loki({}) == []
    assert probe._rows_from_loki({"data": {"result": []}}) == []
    assert probe._rows_from_loki({"data": {"result": [{"values": None}]}}) == []


def test_parse_down_line_extracts_name_and_strips_cycle_counter():
    line = "[2026-07-21T08:37:00] DOWN n8n - 1 active workflow(s) failed (2 cycles)"
    assert probe.parse_down_line(line) == ("n8n", "1 active workflow(s) failed")


def test_parse_down_line_ignores_ok_and_malformed_lines():
    assert probe.parse_down_line("[2026-07-21T08:37:00] OK   n8n - fine") is None
    assert probe.parse_down_line("not a monitor-bridge line") is None


def test_alert_episodes_splits_on_a_silence_gap():
    minute = int(60 * 1e9)
    rows = [
        (0, "backup", "shrank"),
        (5 * minute, "backup", "shrank"),  # same episode (5m <= 30m gap)
        (60 * minute, "backup", "shrank again"),  # new episode (55m gap)
    ]
    eps = probe.alert_episodes(rows, gap_s=1800)
    assert len(eps) == 2
    # newest episode first; its latest msg wins
    assert eps[0]["cycles"] == 1 and eps[0]["msg"] == "shrank again"
    assert eps[1]["cycles"] == 2 and eps[1]["first_ns"] == 0


def test_alert_episodes_keeps_distinct_checks_separate():
    rows = [(0, "backup", "a"), (0, "cpu", "b")]
    eps = probe.alert_episodes(rows, gap_s=1800)
    assert {e["name"] for e in eps} == {"backup", "cpu"}


def test_format_alert_episodes_empty_is_all_clear():
    assert probe.format_alert_episodes([], 7) == "no DOWN alerts in the last 7d"


def test_format_alert_episodes_renders_name_and_msg():
    eps = [{"name": "n8n", "first_ns": 0, "last_ns": 0, "cycles": 1, "msg": "boom"}]
    out = probe.format_alert_episodes(eps, 7)
    assert "1 DOWN episode(s)" in out and "n8n" in out and "boom" in out


#
# These pin the TRANSPORT, deliberately, and the reason is recorded rather than assumed. Three
# assertions already covered `loki_query_url` output and `plan()` argv, and every one of them
# sits UPSTREAM of the defect they would have had to catch: `run_query` built its own URL and
# passed no window, so the formatted path inherited Loki's one-hour server-side default while
# `--dry-run`/`--json` honoured `--since`. Measured before the fix, `--since 3d` returned a
# 60-minute slice — and an empty slice prints "no logs", which reads as health. A fourth
# builder-level assertion would have missed it exactly as the first three did. So: capture the
# url `fetch` is actually called with.


def _capture_fetch(monkeypatch, body='{"data":{"result":[]}}'):
    """Patch out the network and return the list that collects each fetched url."""
    seen = []

    def fake_fetch(url, resolve=None):
        seen.append(url)
        return body

    monkeypatch.setattr(probe, "fetch", fake_fetch)
    monkeypatch.setattr(probe, "sops_extract", lambda key: "example.test")
    monkeypatch.setattr(probe, "metallb_vip", lambda: "10.0.0.240")
    return seen


def _query_params(url):
    from urllib.parse import parse_qs, urlparse

    return parse_qs(urlparse(url).query)


def test_run_query_sends_the_since_window_to_loki(monkeypatch):
    seen = _capture_fetch(monkeypatch)
    ns = probe._build_parser().parse_args(
        ["loki-query", '{job="syslog"}', "--since", "3d", "--limit", "5000"]
    )
    assert probe.run_query(ns) == 0
    params = _query_params(seen[0])
    assert "start" in params and "end" in params
    # The span, not merely the presence of the key: a start pinned to the wrong clock or a
    # window silently clamped to an hour both satisfy a presence check.
    span_s = (int(params["end"][0]) - int(params["start"][0])) / 1e9
    assert abs(span_s - 3 * 86400) < 2


def test_run_query_without_since_sends_no_window():
    assert probe.since_window_ns(None) == (None, None)
    assert probe.since_window_ns("") == (None, None)


def test_since_window_ns_span_matches_the_requested_duration():
    start, end = probe.since_window_ns("2d")
    assert abs((end - start) / 1e9 - 2 * 86400) < 2


def test_run_query_omits_direction_so_limit_keeps_the_newest_lines(monkeypatch):
    # `run_alerts` passes direction=forward because episode reconstruction walks oldest-first.
    # Copying that here would make --limit return the OLDEST N; Loki's default `backward` is
    # what makes it return the newest N, which format_loki then sorts.
    seen = _capture_fetch(monkeypatch)
    ns = probe._build_parser().parse_args(
        ["loki-query", '{job="syslog"}', "--since", "6h"]
    )
    probe.run_query(ns)
    assert "direction=" not in seen[0]


def test_run_query_serves_metric_which_has_no_since_flag(monkeypatch):
    # `metric`'s subparser declares no --since and run_query serves both commands, so a bare
    # `ns.since` on the shared path raises AttributeError and kills every `probe.py metric`.
    seen = _capture_fetch(monkeypatch)
    ns = probe._build_parser().parse_args(["metric", "up"])
    assert not hasattr(ns, "since")
    assert probe.run_query(ns) == 0
    assert "/api/v1/query?" in seen[0]


#
# monitor-bridge polls no Kuma state, so its container log says nothing about the host crons
# that push Kuma directly. Reading only that log left the backup plane's sole DOWN signal
# unrecorded: measured 2026-08-22, 465 `longhorn-backup-health: status=down` lines over 7 days
# appeared in no episode list, while `alerts --check manifest` printed "no DOWN alerts" with
# `monitor_status{monitor_name="Manifest Prune Drift"}` reading 0.

SYSLOG_DOWN = (
    "2026-08-19T13:50:03.382504+00:00 daniel-box longhorn-backup-health: "
    "status=down backed-up volumes stale or missing: homelab/tdarr-server (weekly-d1)"
)
SYSLOG_PUSH_FAILED = (
    "2026-08-16T07:40:04.188815+00:00 daniel-box claude-otel-health: "
    "push failed (status=down: loki 0/1 ready; prometheus not answering queries)"
)
SYSLOG_PUSH_FAILED_TRUNCATED = (
    "2026-08-16T10:36:02.000000+00:00 daniel-box longhorn-backup-health: "
    "push failed (status=down: backups in Error state: backup-4a471c15 backup-9818b9cc"
)


def test_parse_syslog_down_line_reads_the_tag_and_the_message():
    # The rsyslog prefix ("<iso-ts> <host> ") is real and the bare "<tag>: status=down <msg>"
    # shape a reading of the cron scripts suggests never reaches Loki.
    assert probe.parse_syslog_down_line(SYSLOG_DOWN) == (
        "longhorn-backup-health",
        "backed-up volumes stale or missing: homelab/tdarr-server (weekly-d1)",
    )


def test_parse_syslog_down_line_unwraps_a_failed_push():
    # A failed push is the case where syslog is the ONLY record — Kuma never learned — so the
    # prefix stays in the message rather than being discarded.
    name, msg = probe.parse_syslog_down_line(SYSLOG_PUSH_FAILED)
    assert name == "claude-otel-health"
    assert msg == "push failed: loki 0/1 ready; prometheus not answering queries"


def test_parse_syslog_down_line_survives_rsyslog_truncation():
    name, msg = probe.parse_syslog_down_line(SYSLOG_PUSH_FAILED_TRUNCATED)
    assert name == "longhorn-backup-health"
    assert msg.startswith("push failed: backups in Error state:")


def test_parse_syslog_down_line_ignores_up_and_unrelated_lines():
    assert probe.parse_syslog_down_line("not a syslog line") is None
    assert (
        probe.parse_syslog_down_line(
            "2026-08-20T12:40:03+00:00 daniel-box disk-health: status=up / at 22%"
        )
        is None
    )


def _fake_loki(lines):
    return {"data": {"result": [{"values": [[str(ts), line] for ts, line in lines]}]}}


def _route_alert_fetch(monkeypatch, per_query):
    """Serve each alert stream its own body, keyed by the LogQL in the url."""
    import json as _json

    seen = []

    def fake_fetch(url, resolve=None):
        seen.append(url)
        logql = _query_params(url)["query"][0]
        return _json.dumps(_fake_loki(per_query.get(logql, [])))

    monkeypatch.setattr(probe, "fetch", fake_fetch)
    monkeypatch.setattr(probe, "sops_extract", lambda key: "example.test")
    monkeypatch.setattr(probe, "metallb_vip", lambda: "10.0.0.240")
    return seen


def test_alerts_queries_the_host_cron_stream_as_well_as_the_bridge(monkeypatch):
    seen = _route_alert_fetch(monkeypatch, {})
    ns = probe._build_parser().parse_args(["alerts", "--days", "3"])
    assert probe.run_alerts(ns) == 0
    queries = [_query_params(u)["query"][0] for u in seen]
    assert probe.ALERT_LOGQL in queries
    assert probe.SYSLOG_ALERT_LOGQL in queries


def test_alerts_surfaces_a_host_cron_episode_the_bridge_stream_cannot_see(
    monkeypatch, capsys
):
    """The acceptance case: monitor-bridge's stream is EMPTY and the episode still appears."""
    minute = int(60 * 1e9)
    _route_alert_fetch(
        monkeypatch,
        {
            probe.ALERT_LOGQL: [],
            probe.SYSLOG_ALERT_LOGQL: [
                (minute, SYSLOG_DOWN),
                (11 * minute, SYSLOG_DOWN),
            ],
        },
    )
    ns = probe._build_parser().parse_args(["alerts", "--days", "3"])
    assert probe.run_alerts(ns) == 0
    out = capsys.readouterr().out
    assert "1 DOWN episode(s)" in out
    assert "longhorn-backup-health" in out
    assert "backed-up volumes stale or missing" in out


def test_alerts_check_filter_matches_a_host_cron_tag(monkeypatch, capsys):
    # `--check` has to keep working across both streams, which it does only because the syslog
    # tag is a machine name like monitor-bridge's own check names. Kuma's monitor_name is a
    # DISPLAY name ("Manifest Prune Drift"), so an episode set keyed on it would silently
    # break `--check manifest-prune-check` for every caller.
    minute = int(60 * 1e9)
    _route_alert_fetch(
        monkeypatch,
        {
            probe.ALERT_LOGQL: [
                (
                    minute,
                    "[2026-08-19T13:50:03] DOWN n8n - 1 workflow failed (2 cycles)",
                )
            ],
            probe.SYSLOG_ALERT_LOGQL: [(minute, SYSLOG_DOWN)],
        },
    )
    ns = probe._build_parser().parse_args(
        ["alerts", "--days", "3", "--check", "longhorn"]
    )
    assert probe.run_alerts(ns) == 0
    out = capsys.readouterr().out
    assert "longhorn-backup-health" in out
    assert "n8n" not in out


def test_alerts_dry_run_prints_a_command_per_stream(monkeypatch, capsys):
    monkeypatch.setattr(probe, "sops_extract", lambda key: "example.test")
    monkeypatch.setattr(probe, "metallb_vip", lambda: "10.0.0.240")
    ns = probe._build_parser().parse_args(["--dry-run", "alerts", "--days", "3"])
    assert probe.run_alerts(ns) == 0
    out = capsys.readouterr().out
    assert out.count("query_range") == len(probe.ALERT_SOURCES)


#
# Longhorn reports a backup `Completed` once its metadata is written, so "Completed" is not
# evidence the DATA reached B2. These cover the distinction the command exists to make, and
# the credential-handling that keeps it safe to run.

LSF = [
    "backupstore/volumes/aa/bb/pvc-authelia/volume.cfg;120",
    "backupstore/volumes/aa/bb/pvc-authelia/backups/backup_x.cfg;340",
    "backupstore/volumes/aa/bb/pvc-authelia/blocks/1a/2b/deadbeef.blk;2097152",
    "backupstore/volumes/aa/bb/pvc-authelia/blocks/1a/2c/cafebabe.blk;1048576",
    "backupstore/volumes/cc/dd/pvc-bento/blocks/0f/0e/f00d.blk;524288",
]


def test_b2_credentials_travel_in_the_stdin_config_not_argv():
    """argv is visible in `ps`, so the application key must only ever reach curl's stdin.

    The old Docker implementation kept the key out of argv by having `docker exec -e VAR`
    inherit it; curl's `--config -` is the same guard by the route the rest of this file
    already uses for HA and the *arr apps.
    """
    body = probe.b2_authorize_config("keyid123", "appkey456")
    assert 'user = "keyid123:appkey456"' in body
    assert probe.B2_AUTHORIZE_URL in body


def test_b2_list_config_carries_the_token_as_a_header_and_scopes_the_prefix():
    body = probe.b2_list_files_config("https://api.example", "tok", "bid", "longhorn")
    assert 'header = "Authorization: tok"' in body
    assert "prefix=longhorn%2F" in body and "bucketId=bid" in body


def test_b2_longhorn_lines_strips_the_prefix_and_pages():
    """Paths must come back RELATIVE to the prefix, as rclone's lsf produced them.

    B2 returns absolute names (`longhorn/backupstore/...`). Leaving them absolute matches
    none of parse_longhorn_listing's patterns, so a perfectly healthy bucket would report
    "no Longhorn backup objects" — a false data-loss alarm.
    """
    pages = [
        {
            "apiInfo": {
                "storageApi": {"apiUrl": "https://api.example", "bucketId": "b"}
            },
            "authorizationToken": "tok",
            "accountId": "acct",
        },
        {
            "files": [
                {
                    "fileName": "longhorn/backupstore/volumes/aa/bb/pvc-x/blocks/1/2/a.blk",
                    "contentLength": 2097152,
                }
            ],
            "nextFileName": "more",
        },
        {
            "files": [
                {
                    "fileName": "longhorn/backupstore/volumes/aa/bb/pvc-x/volume.cfg",
                    "contentLength": 120,
                }
            ]
        },
    ]
    calls = iter(pages)
    lines = probe.b2_longhorn_lines(
        "k", "s", "bucket", "longhorn", _call=lambda _body: next(calls)
    )
    assert lines == [
        "backupstore/volumes/aa/bb/pvc-x/blocks/1/2/a.blk;2097152",
        "backupstore/volumes/aa/bb/pvc-x/volume.cfg;120",
    ]
    # The whole point: these lines survive the parser that the real command feeds them to.
    vols = probe.parse_longhorn_listing(lines)
    assert vols["pvc-x"]["blocks"] == 1 and vols["pvc-x"]["cfgs"] == 1


def test_b2_longhorn_command_does_not_shell_out_to_docker_or_rclone():
    """The regression this rewrite exists for.

    `probe.py b2-longhorn` shelled out to `docker exec kopia rclone ...` and died with
    FileNotFoundError on both k3s nodes from the day Docker was removed (2026-08-14) —
    while the tests stayed green because they only covered the argv builder and the parser.
    Neither binary exists on these hosts, so naming them here is a dead path by definition.
    """
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["stdin"] = kwargs.get("input", "")

        class Result:
            returncode = 0
            stdout = "{}"
            stderr = ""

        return Result()

    real_run = probe.subprocess.run
    probe.subprocess.run = fake_run
    try:
        probe.b2_curl('url = "https://api.example"\n')
    finally:
        probe.subprocess.run = real_run

    assert seen["argv"][0] == "curl"
    assert "docker" not in seen["argv"] and "rclone" not in seen["argv"]
    # The url/credentials reach curl through stdin, so argv stays free of both.
    assert seen["stdin"].startswith("url = ")

    # And no `"docker"` argv literal survives anywhere in this section's executable code.
    with open(_MOD) as fh:
        section = fh.read().split("# B2 / Longhorn backup objects")[1]
    code = "\n".join(
        line for line in section.splitlines() if not line.strip().startswith("#")
    )
    assert '"docker"' not in code


def test_parse_longhorn_listing_separates_data_from_metadata():
    vols = probe.parse_longhorn_listing(LSF)
    assert vols["pvc-authelia"]["blocks"] == 2
    assert vols["pvc-authelia"]["block_bytes"] == 2097152 + 1048576
    assert vols["pvc-authelia"]["cfgs"] == 2
    assert vols["pvc-bento"]["blocks"] == 1


def test_parse_longhorn_listing_ignores_unrelated_and_malformed_lines():
    vols = probe.parse_longhorn_listing(
        ["", "   ", "kopia/p1234.f;99", "backupstore/volumes/aa;10", "no-semicolon"]
    )
    assert vols == {}


def test_format_longhorn_summary_fails_when_a_volume_has_no_data_blocks():
    """Metadata without blocks is the silent-corruption case worth exiting non-zero on."""
    vols = {"pvc-empty": {"blocks": 0, "block_bytes": 0, "cfgs": 3}}
    text, code = probe.format_longhorn_summary(vols)
    assert code == 1
    assert "NO DATA BLOCKS" in text and "pvc-empty" in text


def test_format_longhorn_summary_passes_when_every_volume_has_blocks():
    text, code = probe.format_longhorn_summary(probe.parse_longhorn_listing(LSF))
    assert code == 0
    assert "pvc-authelia" in text and "NO DATA BLOCKS" not in text


def test_format_longhorn_summary_treats_no_objects_as_failure():
    text, code = probe.format_longhorn_summary({})
    assert code == 1 and "no Longhorn backup objects" in text


def test_parse_backup_budget_prices_a_prune_by_directories_not_blocks():
    """A prune's cost is one ListObjects per block directory, so two blocks sharing a
    second-level directory cost less than two that do not."""
    vols = probe.parse_backup_budget(LSF)
    # pvc-authelia: blocks/ + 1a/ + (1a,2b) + (1a,2c) = 4
    assert vols["pvc-authelia"]["prune"] == 4
    assert vols["pvc-authelia"]["blocks"] == 2
    assert vols["pvc-authelia"]["backups"] == 1
    # volume.cfg is not a backup, so it must not inflate the retention count.
    assert vols["pvc-bento"]["backups"] == 0
    assert vols["pvc-bento"]["prune"] == 3


def test_format_backup_budget_flags_a_shard_over_the_daily_cap():
    over = probe.B2_CLASS_C_DAILY_CAP - probe.B2_BUDGET_RESERVE + 1
    vols = {"pvc-big": {"prune": over, "blocks": 9000, "backups": 4}}
    text, code = probe.format_backup_budget(vols, {"pvc-big": "weekly-backup-d2"})
    assert code == 1
    assert "OVER BUDGET" in text and "weekly-backup-d2" in text


def test_stranded_counts_backups_the_current_tier_does_not_own():
    """Stranded means "no job will ever prune this", not "past retain".

    Longhorn's retain counts only a job's OWN backups, so a daily-era backup on a volume that
    has since moved to a weekday shard is pruned by nothing, ever. Until 2026-08-19 this was
    computed as `max(0, backups - retain)`, which under-reported the live cluster by 4.7x — 7
    against a true 33 — on the number an operator reads before deciding what to delete.
    """
    vols = {"pvc-moved": {"prune": 10, "blocks": 100, "backups": 5}}
    owners = {"pvc-moved": {"daily-backup": 4, "weekly-backup-d2": 1}}
    text, _ = probe.format_backup_budget(
        vols, {"pvc-moved": "weekly-backup-d2"}, retain=2, owners=owners
    )
    assert "4 stranded backup(s)" in text, text


def test_backups_the_current_tier_owns_are_not_stranded_even_past_retain():
    """The owning job prunes them on its next run, so they are queued, not abandoned."""
    vols = {"pvc-busy": {"prune": 10, "blocks": 100, "backups": 5}}
    owners = {"pvc-busy": {"weekly-backup-d2": 5}}
    text, _ = probe.format_backup_budget(
        vols, {"pvc-busy": "weekly-backup-d2"}, retain=2, owners=owners
    )
    assert "stranded" not in text, text


def test_stranded_falls_back_to_zero_without_ownership_data():
    """No owners map means nothing is PROVEN stranded — never guess high and prompt a delete."""
    vols = {"pvc-x": {"prune": 10, "blocks": 100, "backups": 5}}
    text, _ = probe.format_backup_budget(vols, {"pvc-x": "weekly-backup-d2"}, retain=2)
    assert "stranded" not in text, text


def test_format_backup_budget_does_not_charge_a_day_for_an_unscheduled_volume():
    """A volume with no recurring job never runs a backup and so never prunes — charging its
    blocks to a shard would read as an over-budget day that cannot actually happen."""
    vols = {"pvc-idle": {"prune": 99999, "blocks": 9000, "backups": 3}}
    text, code = probe.format_backup_budget(vols, {"pvc-idle": "no-backup"})
    assert code == 0
    assert "never pruned" in text and "pvc-idle" in text


SPEND_LOG = [
    (
        1,
        '[pvc-1c0e18da-dd0a-4059-af81-f5f346c7eabc-r-9d333575] time="..." '
        'msg="Created snapshot changed blocks: 104 mappings, 104 blocks and 75 new blocks"',
    ),
    (
        2,
        '[pvc-00d8210a-e38d-49f9-ba22-3aff333f59ab-r-b0d3cf84] time="..." '
        'msg="Created snapshot changed blocks: 77 mappings, 77 blocks and 67 new blocks"',
    ),
    (3, 'time="..." msg="Performing delta block backup"'),
]


def test_parse_duration_seconds_accepts_the_documented_forms():
    assert probe.parse_duration_seconds("30m") == 1800
    assert probe.parse_duration_seconds("6h") == 21600
    assert probe.parse_duration_seconds("2d") == 172800
    assert probe.parse_duration_seconds("1w") == 604800


def test_parse_duration_seconds_rejects_junk_rather_than_defaulting():
    """A silently-ignored duration would query Loki's one-hour default and report an empty
    window as 'nothing ran', which is the failure this flag exists to prevent."""
    for bad in ("6", "h", "6y", "-2d", "", "6 h"):
        with pytest.raises(SystemExit):
            probe.parse_duration_seconds(bad)


def test_parse_backup_spend_counts_delta_blocks_per_volume():
    """`blocks` is the delta Longhorn walks, and it HeadObjects each one — so that count is the
    backup's Class B cost. `new blocks` is what it uploaded, which is Class A and free."""
    vols = probe.parse_backup_spend(SPEND_LOG)
    assert vols["pvc-1c0e18da-dd0a-4059-af81-f5f346c7eabc"]["blocks"] == 104
    assert vols["pvc-1c0e18da-dd0a-4059-af81-f5f346c7eabc"]["new_blocks"] == 75
    assert vols["pvc-00d8210a-e38d-49f9-ba22-3aff333f59ab"]["backups"] == 1
    # The unrelated progress line must not be counted as a backup.
    assert len(vols) == 2


def test_parse_backup_spend_keeps_lines_whose_replica_prefix_was_trimmed():
    """Dropping an unattributable line would understate spend, and understating is the failure
    mode that matters — the cap does not care which volume it was."""
    vols = probe.parse_backup_spend(
        [
            (
                1,
                'msg="Created snapshot changed blocks: 9 mappings, 9 blocks and 2 new blocks"',
            )
        ]
    )
    assert vols["unattributed"]["blocks"] == 9


def test_format_backup_spend_totals_and_says_when_the_window_was_empty():
    text = probe.format_backup_spend(probe.parse_backup_spend(SPEND_LOG), "6h")
    assert "backups over 6h: 181 Class B measured" in text
    empty = probe.format_backup_spend({}, "6h")
    assert "no backups logged" in empty and "widen --since" in empty


def test_parse_b2_ledger_totals_per_tool_and_skips_malformed_lines():
    tools = probe.parse_b2_ledger(
        [
            "2026-08-17T12:00:00Z\tdrain\t972\t59\t5\tretain 2",
            "2026-08-17T13:00:00Z\tdrain\t179\t5\t4\tradarr",
            "2026-08-17T14:00:00Z\tb2-budget\t0\t0\t5\t4 pages",
            "not a ledger line",
            "2026-08-17T15:00:00Z\tdrain\tnot\tnumbers\there\t",
        ]
    )
    assert tools["drain"] == {"runs": 2, "class_a": 1151, "class_b": 64, "class_c": 9}
    assert tools["b2-budget"]["class_c"] == 5
    assert "not a ledger line" not in tools


def test_record_b2_spend_never_raises_when_the_ledger_is_unwritable(monkeypatch):
    """A ledger failure must not fail the real work — the accounting is secondary to the
    operation it is accounting for."""
    monkeypatch.setattr(probe, "B2_LEDGER_DIR", "/proc/cannot/create/this")
    probe.record_b2_spend("drain", class_c=5)  # must not raise


def test_record_then_read_round_trips(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "B2_LEDGER_DIR", str(tmp_path))
    probe.record_b2_spend("drain", class_a=100, class_b=59, class_c=5, note="retain 2")
    probe.record_b2_spend("b2-budget", class_c=5)
    tools = probe.read_b2_ledger()
    assert tools["drain"]["class_b"] == 59
    assert tools["b2-budget"]["class_c"] == 5


def test_b2_longhorn_lines_reports_pages_plus_the_authorize_as_class_c():
    """Each page is one b2_list_file_names and the authorize before them is billable too, so a
    two-page listing costs three Class C — the number the ledger needs."""
    pages = [
        {
            "files": [{"fileName": "longhorn/a", "contentLength": 1}],
            "nextFileName": "b",
        },
        {"files": [{"fileName": "longhorn/b", "contentLength": 2}]},
    ]
    calls = []

    def fake(_config):
        if not calls:
            calls.append(1)
            return {
                "apiInfo": {"storageApi": {"apiUrl": "https://api", "bucketId": "bid"}},
                "authorizationToken": "t",
            }
        return pages.pop(0)

    stats = {}
    probe.b2_longhorn_lines("k", "s", "bucket", _call=fake, _stats=stats)
    assert stats == {"class_c": 3, "pages": 2}


def test_format_backup_spend_shows_maintenance_and_never_sums_the_two_windows():
    """Backups span --since; the ledger covers the UTC day. A combined total would match
    neither, so the report must keep them apart."""
    text = probe.format_backup_spend(
        probe.parse_backup_spend(SPEND_LOG),
        "6h",
        ledger={"drain": {"runs": 2, "class_a": 0, "class_b": 64, "class_c": 9}},
    )
    assert "backups over 6h: 181 Class B measured" in text
    assert "drain" in text and "64 Class B" in text
    assert "245" not in text  # 181 + 64 must not appear as a combined figure


def test_format_backup_budget_flags_a_b2_volume_left_on_the_daily_tier():
    """A PVC provisioned from the longhorn StorageClass lands in `default` until a deploy
    reconciles its label, which on B2 means a prune every night against a weekly budget."""
    vols = {"pvc-new": {"prune": 300, "blocks": 200, "backups": 4}}
    text, code = probe.format_backup_budget(vols, {"pvc-new": "default"})
    assert code == 1
    assert "ON THE DAILY TIER AND ON B2" in text and "pvc-new" in text


def test_format_backup_budget_reports_stranded_backups_not_pending_deletes():
    """Stranded backups are abandoned, not queued. Longhorn enforces retain only when the owning
    job runs against a volume still in its groups, counting only its own backups — so a volume
    that moved tier keeps its old backups forever and only the reaper clears them.

    This asserted `backups - retain` until 2026-08-19, which is a different quantity and made
    the check ratify the bug: 11 backups against retain 4 read as 7 stranded, when the answer
    depends entirely on who owns them. Here the d5 job owns 2, so the other 9 are the strays.
    """
    vols = {"pvc-a": {"prune": 100, "blocks": 50, "backups": 11}}
    owners = {"pvc-a": {"daily-backup": 9, "weekly-backup-d5": 2}}
    text, code = probe.format_backup_budget(
        vols, {"pvc-a": "weekly-backup-d5"}, retain=4, owners=owners
    )
    assert code == 0
    assert "9 stranded backup(s)" in text and "reaper" in text


def test_no_cluster_route_carries_the_retired_k8s_suffix():
    """The `-k8s` suffix retired 2026-08-15 (870723e8), but probe.py kept building it for
    another five hours: every cluster subcommand 404'd against Traefik's no-Host-match while
    the fixtures below asserted the stale name, so CI ratified the break. Assert on the
    hostnames plan() actually asks for, so a reintroduced suffix fails here first."""
    asked = []

    def record(hostname):
        asked.append(hostname)
        return fake_k8s_endpoint(hostname)

    for argv in (
        ["metric", "up"],
        ["targets"],
        ["loki-labels"],
        ["loki-query", '{job="x"}'],
        ["scrutiny"],
    ):
        probe.plan(argv, fake_resolve, record)

    assert asked, "expected plan() to route these subcommands through k8s_endpoint"
    assert not [h for h in asked if h.endswith("-k8s")]


TEMPLATE_SAMPLE = """\
stringData:
  discord.json: |
    {"type": "notification", "name": "Homelab Alerts", "active": true}
  root-disk.json: |
    {"type": "push", "name": "Root Disk", "interval": 60, "push_token": "x"}
  peer-backup.json: |
    {"type": "push", "name": "WG Pi Peer Backup", "interval": 216000, "push_token": "x"}
  grafana.json: |
    {"type": "http", "name": "k3s Grafana", "url": "https://g.example", "interval": 60}
{% if etcd_snapshot_push_token | default('') %}
  etcd.json: |
    {"type": "push", "name": "Off-box etcd Snapshot", "interval": 90000, "push_token": "x"}
{% endif %}
"""


def test_parse_declared_monitors_reads_names_types_and_gating():
    declared = probe.parse_declared_monitors(TEMPLATE_SAMPLE)
    # Notifications are not monitors and never appear in monitor_status — counting them would
    # make every run report two phantom missing entries.
    assert "Homelab Alerts" not in declared
    assert declared["Root Disk"] == {
        "type": "push",
        "interval": 60,
        "gated": False,
        "gate": None,
    }
    assert declared["k3s Grafana"]["type"] == "http"
    assert declared["Off-box etcd Snapshot"]["gated"] is True
    # The variable is captured, not just the fact of being gated — that name is what lets the
    # caller resolve the secret instead of assuming it is unset.
    assert declared["Off-box etcd Snapshot"]["gate"] == "etcd_snapshot_push_token"


def test_kuma_drift_reports_a_declared_monitor_that_is_not_live():
    # The 2026-08-20 case: the tile is absent from the exporter, not down, so `monitors`
    # reported 81/81 up for a day. Long-uptime Kuma, so PENDING cannot be the explanation.
    declared = probe.parse_declared_monitors(TEMPLATE_SAMPLE)
    live = {"Root Disk", "k3s Grafana"}
    text, code = probe.format_kuma_drift(declared, live, 86400 * 3)
    assert code == 1
    assert "WG Pi Peer Backup: declared, not live" in text


def test_kuma_drift_calls_a_push_monitor_pending_inside_its_own_interval():
    # Kuma exports a monitor only after it beats, so a restart empties every push series. A
    # monitor whose interval has not elapsed since the restart is not yet due — flagging it
    # would make this check fail after every deploy.
    declared = probe.parse_declared_monitors(TEMPLATE_SAMPLE)
    live = {"k3s Grafana"}
    text, code = probe.format_kuma_drift(declared, live, 30)
    assert code == 0
    assert "no beat due yet" in text
    assert "declared, not live" not in text


def test_kuma_drift_treats_every_type_as_pending_after_a_restart():
    # The first live run of this check reported 58 monitors missing 88 seconds into a rollout.
    # Kuma's exporter emits a monitor only after it beats, and that applies to http/port/dns
    # tiles too — restricting the pending rule to push monitors made a routine deploy look like
    # mass drift. The slack covers the exporter's and Prometheus's scrape lag on top.
    declared = probe.parse_declared_monitors(TEMPLATE_SAMPLE)
    text, code = probe.format_kuma_drift(declared, set(), 88)
    assert code == 0
    assert "k3s Grafana: no beat due yet" in text


def test_kuma_drift_fails_loud_when_the_pod_age_is_unreadable():
    # Same rule as `health`'s unreadable restart time: an unknown age must not silently excuse
    # a missing monitor, or the check reports green exactly when it cannot tell.
    declared = probe.parse_declared_monitors(TEMPLATE_SAMPLE)
    text, code = probe.format_kuma_drift(declared, {"k3s Grafana"}, None)
    assert code == 1
    assert "Root Disk: declared, not live" in text


def test_kuma_drift_reports_a_live_monitor_nobody_declared():
    # `kubectl apply` leaves orphaned objects behind, and AutoKuma's on_delete=delete only
    # removes what it still tracks — a monitor whose declaration was dropped can outlive it.
    declared = probe.parse_declared_monitors(TEMPLATE_SAMPLE)
    live = {"Root Disk", "WG Pi Peer Backup", "k3s Grafana", "Retired Tile"}
    text, code = probe.format_kuma_drift(declared, live, 86400)
    assert code == 1
    assert "Retired Tile: live, not declared" in text


def test_kuma_drift_skips_a_monitor_whose_gate_is_genuinely_unset():
    declared = probe.parse_declared_monitors(TEMPLATE_SAMPLE)
    live = {"Root Disk", "WG Pi Peer Backup", "k3s Grafana"}
    text, code = probe.format_kuma_drift(
        declared, live, 86400, gate_states={"etcd_snapshot_push_token": False}
    )
    assert code == 0
    assert "Off-box etcd Snapshot" in text
    assert "genuinely unset" in text


def test_kuma_drift_reports_drift_when_the_gate_is_set_but_the_monitor_is_absent():
    """The 2026-08-22 case, and the reason `gate` exists.

    etcd_snapshot_push_token was set (32 chars, in the rotation registry since 2026-07-04) and
    Off-box etcd Snapshot was not live — and the old check called that correctly skipped. A
    gated monitor that vanishes was invisible twice: absent from the exporter, and excused by
    the drift check written to catch exactly that.
    """
    declared = probe.parse_declared_monitors(TEMPLATE_SAMPLE)
    live = {"Root Disk", "WG Pi Peer Backup", "k3s Grafana"}
    # Past the monitor's own 90000s interval, so `pending` cannot absorb it — a gate-set
    # monitor inside its interval is still legitimately pending, not drift.
    text, code = probe.format_kuma_drift(
        declared, live, 86400 * 3, gate_states={"etcd_snapshot_push_token": True}
    )
    assert code == 1
    assert "Off-box etcd Snapshot: declared, not live" in text
    assert "genuinely unset" not in text


def test_kuma_drift_says_so_when_a_gate_cannot_be_read():
    """An unreadable gate and an unset one must not look alike — that equivalence is what let
    the case above stay silent. Unreadable does not fail the exit code (no age key on this
    host is a normal state), but it is named rather than swallowed."""
    declared = probe.parse_declared_monitors(TEMPLATE_SAMPLE)
    live = {"Root Disk", "WG Pi Peer Backup", "k3s Grafana"}
    text, code = probe.format_kuma_drift(
        declared, live, 86400, gate_states={"etcd_snapshot_push_token": None}
    )
    assert code == 0
    assert "could not be read" in text
    assert "genuinely unset" not in text
