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


_MOD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe.py")
_spec = importlib.util.spec_from_file_location("probe", _MOD)
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)

# Fake resolver: maps container name -> a recognizable IP. A wrong container name
# raises KeyError, so a misrouted subcommand fails loudly.
IPS = {"prometheus": "10.0.0.1", "loki": "10.0.0.2", "scrutiny": "10.0.0.3"}
fake_resolve = IPS.__getitem__


# --- URL builders -----------------------------------------------------------


def test_prom_query_url_encodes_promql():
    url = probe.prom_query_url("10.0.0.1", "up == 0")
    assert url == "http://10.0.0.1:9090/api/v1/query?query=up+%3D%3D+0"


def test_prom_targets_url():
    assert probe.prom_targets_url("10.0.0.1") == "http://10.0.0.1:9090/api/v1/targets"


def test_loki_labels_url():
    assert (
        probe.loki_labels_url("10.0.0.2") == "http://10.0.0.2:3100/loki/api/v1/labels"
    )


def test_loki_query_url_encodes_logql_and_limit():
    url = probe.loki_query_url("10.0.0.2", '{job="x"}', 50)
    assert (
        url
        == "http://10.0.0.2:3100/loki/api/v1/query_range?query=%7Bjob%3D%22x%22%7D&limit=50"
    )


def test_scrutiny_url():
    assert probe.scrutiny_url("10.0.0.3") == "http://10.0.0.3:8080/api/summary"


def test_pi_url():
    assert probe.pi_url("fs") == "http://daniel-pi.lan:61208/api/4/fs"


# --- low-level argv / parsing helpers ---------------------------------------


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


# --- plan(): routing for each subcommand ------------------------------------


def test_plan_metric_resolves_prometheus():
    stages = probe.plan(["metric", "up == 0"], fake_resolve)
    assert stages == [
        probe.curl_argv("http://10.0.0.1:9090/api/v1/query?query=up+%3D%3D+0")
    ]


def test_plan_targets_resolves_prometheus():
    stages = probe.plan(["targets"], fake_resolve)
    assert stages == [probe.curl_argv("http://10.0.0.1:9090/api/v1/targets")]


def test_plan_loki_labels_resolves_loki():
    stages = probe.plan(["loki-labels"], fake_resolve)
    assert stages == [probe.curl_argv("http://10.0.0.2:3100/loki/api/v1/labels")]


def test_plan_loki_query_with_limit():
    stages = probe.plan(["loki-query", '{job="x"}', "--limit", "50"], fake_resolve)
    assert stages == [
        probe.curl_argv(probe.loki_query_url("10.0.0.2", '{job="x"}', 50))
    ]


def test_plan_scrutiny_resolves_scrutiny():
    stages = probe.plan(["scrutiny"], fake_resolve)
    assert stages == [probe.curl_argv("http://10.0.0.3:8080/api/summary")]


def test_plan_pi_does_not_resolve_docker():
    # Pi glances is reached by hostname, so the resolver must NOT be consulted.
    def boom(_):
        raise AssertionError("pi must not resolve a container IP")

    stages = probe.plan(["pi", "fs"], boom)
    assert stages == [probe.curl_argv("http://daniel-pi.lan:61208/api/4/fs")]


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


# --- health: container state + healthcheck rollup ---------------------------


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


# --- ha subcommand: URL builders --------------------------------------------


def test_ha_state_url():
    assert (
        probe.ha_state_url("10.1.2.3", "fan.tower_fan")
        == "http://10.1.2.3:8123/api/states/fan.tower_fan"
    )


def test_ha_get_url_bare_path():
    assert (
        probe.ha_get_url("10.1.2.3", "error_log")
        == "http://10.1.2.3:8123/api/error_log"
    )


def test_ha_get_url_normalizes_leading_slash_and_api_prefix():
    # A user may type any of these; all mean the same endpoint.
    for path in ("error_log", "/error_log", "api/error_log", "/api/error_log"):
        assert probe.ha_get_url("h", path) == "http://h:8123/api/error_log"


# --- ha subcommand: match_automation (the alias-slug-vs-id trap) -------------


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


# --- ha subcommand: curl argv must never carry the token ---------------------


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


# --- ha subcommand: output formatters ---------------------------------------


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


# --- WebSocket frame codec --------------------------------------------------


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


# --- ha why / ha trace: format_trace parser ----------------------------------

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


# --- metric / loki-query output formatters ----------------------------------
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


# --- arr subcommand: read-only *arr API GET (key from SOPS, fed via stdin) ----
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


# --- alerts (monitor-bridge DOWN history) -----------------------------------


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
