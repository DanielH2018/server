"""`probe.py ha ...`: live Home Assistant state, and the alias-slug-vs-id trap.

An automation's alias slug is not its id, and looking one up by the other returns nothing rather
than erroring — which reads as "the automation did not fire". `match_automation` is what closes
that, and the WebSocket codec and trace parser are what `ha why` / `ha trace` are built on.
"""

import os

import pytest

import probe_core as core
import probe_ha as ha


def test_ha_base_builds_on_ha_host(monkeypatch):
    # ha_host() decrypts the domain from SOPS; stub it — CI has no age key.
    monkeypatch.setattr(core, "sops_extract", lambda key: "example.test")
    assert core.ha_host() == "home-assistant.local.example.test"
    assert core.ha_base() == "https://home-assistant.local.example.test"


def test_ha_resolve_pins_vip(monkeypatch):
    # Since the bridge teardown (slice-7 BT4) host-shell DNS for the .local name rides the
    # Cloudflare wildcard, so every HA call pins the name to the ingress VIP.
    monkeypatch.setattr(core, "sops_extract", lambda key: "example.test")
    monkeypatch.setattr(core, "metallb_vip", lambda: "10.0.0.240")
    assert core.ha_resolve() == "home-assistant.local.example.test:443:10.0.0.240"


def test_ha_curl_argv_resolve_precedes_url():
    argv = core.ha_curl_argv("https://h/api/states/x", resolve="h:443:10.0.0.240")
    assert argv[-1] == "https://h/api/states/x"
    assert argv[argv.index("--resolve") + 1] == "h:443:10.0.0.240"


def test_ha_state_url():
    # The base is the bridge URL since slice-5 B3 (HA in the cluster — no container to inspect).
    assert (
        ha.ha_state_url("https://ha.example", "fan.tower_fan")
        == "https://ha.example/api/states/fan.tower_fan"
    )


def test_ha_get_url_bare_path():
    assert (
        ha.ha_get_url("https://ha.example", "error_log")
        == "https://ha.example/api/error_log"
    )


def test_ha_get_url_normalizes_leading_slash_and_api_prefix():
    # A user may type any of these; all mean the same endpoint.
    for path in ("error_log", "/error_log", "api/error_log", "/api/error_log"):
        assert ha.ha_get_url("https://h", path) == "https://h/api/error_log"


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
    m = ha.match_automation(_HA_STATES, "bedroom_presence_on")
    assert m["entity_id"] == "automation.bedroom_presence_on"


def test_match_automation_by_id_when_alias_differs():
    # Querying the id finds the entity even though its slug differs — the whole point.
    m = ha.match_automation(_HA_STATES, "bedroom_fan_temperature")
    assert m["entity_id"] == "automation.bedroom_fan_temperature_control"


def test_match_automation_by_friendly_name_slug():
    m = ha.match_automation(_HA_STATES, "bedroom_fan_temperature_control")
    assert m["attributes"]["id"] == "bedroom_fan_temperature"


def test_match_automation_accepts_full_entity_id():
    m = ha.match_automation(_HA_STATES, "automation.bedroom_presence_on")
    assert m["attributes"]["id"] == "presence_1"


def test_match_automation_none_for_unknown():
    assert ha.match_automation(_HA_STATES, "does_not_exist") is None


def test_match_automation_ignores_non_automation_domain():
    # "tower_fan" is a fan, not an automation — must not match.
    assert ha.match_automation(_HA_STATES, "tower_fan") is None


def test_ha_curl_argv_reads_header_from_stdin_config():
    argv = core.ha_curl_argv("http://h:8123/api/states/x")
    assert "--config" in argv and "-" in argv
    assert argv[-1] == "http://h:8123/api/states/x"


def test_ha_curl_argv_carries_no_token():
    # Regression guard: no element of argv may carry the bearer token (ps/history).
    argv = core.ha_curl_argv("http://h:8123/api/states/x")
    assert not any("Bearer" in a or "Authorization" in a for a in argv)


def test_ha_curl_config_has_bearer_header():
    cfg = ha.ha_curl_config("SECRET_TOKEN")
    assert 'header = "Authorization: Bearer SECRET_TOKEN"' in cfg


def test_format_ha_state_shows_entity_state_and_name():
    obj = {
        "entity_id": "fan.tower_fan",
        "state": "on",
        "attributes": {"friendly_name": "Tower Fan"},
        "last_changed": "2026-06-20T12:00:00+00:00",
        "last_updated": "2026-06-20T12:00:00+00:00",
    }
    out = ha.format_ha_state(obj)
    assert "fan.tower_fan" in out and "on" in out and "Tower Fan" in out
    assert "last_changed=2026-06-20T12:00:00+00:00" in out


def test_format_ha_automation_includes_id_and_last_triggered():
    obj = _auto("automation.bedroom_presence_on", "presence_1", "Bedroom Presence On")
    out = ha.format_ha_automation(obj)
    assert "automation.bedroom_presence_on" in out
    assert "presence_1" in out
    assert "last_triggered=2026-06-20T12:00:00+00:00" in out
    assert "Bedroom Presence On" in out


def test_ws_encode_is_masked_client_text_frame():
    frame = ha._ws_encode("hello")
    assert frame[0] == 0x81  # FIN + text opcode
    assert frame[1] == 0x80 | 5  # mask bit + 5-byte length
    mask, body = frame[2:6], frame[6:]
    assert bytes(b ^ mask[i % 4] for i, b in enumerate(body)) == b"hello"


def test_ws_encode_extended_length_126():
    payload = "x" * 200
    frame = ha._ws_encode(payload)
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

    assert ha._ws_read_frame(recv_exact) == '{"type":"auth_ok"}'


def test_ws_read_frame_decodes_extended_length():
    payload = b"y" * 300
    raw = bytes([0x81, 126]) + (300).to_bytes(2, "big") + payload
    pos = [0]

    def recv_exact(n):
        chunk = raw[pos[0] : pos[0] + n]
        pos[0] += n
        return chunk

    assert ha._ws_read_frame(recv_exact) == "y" * 300


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
    out = ha.format_trace(_TRACE_BLOCKED)
    assert "binary_sensor.aqara_fp300_presence" in out
    assert "condition/0" in out
    assert "FAIL" in out


def test_format_trace_none_is_explained():
    assert "no stored trace" in ha.format_trace(None)


def test_format_trace_reports_error():
    out = ha.format_trace({"trigger": {}, "trace": {}, "error": "boom"})
    assert "boom" in out


def test_expected_automation_ids_matches_top_level_only():
    from probe_ha import expected_automation_ids

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
    from probe_ha import automation_load_errors

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
        "automation b_missing is defined in files/automations/ but did not load",
        "automation c_unavailable loaded but is unavailable (config error at load)",
    ]


def test_automation_load_errors_clean_when_all_loaded():
    from probe_ha import automation_load_errors

    expected = {"a", "b"}
    live = [
        {"entity_id": "automation.a", "state": "on", "attributes": {"id": "a"}},
        {"entity_id": "automation.b", "state": "off", "attributes": {"id": "b"}},
    ]
    assert automation_load_errors(expected, live) == []


def test_automation_load_errors_tolerates_missing_attributes():
    # A live entity with attributes null or absent must be skipped, not raise — exercises the
    # `(a.get("attributes") or {})` guard. (No expected id matches them, so they're ignored.)
    from probe_ha import automation_load_errors

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
    from probe_ha import snapshot_entity_ids

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
    from probe_ha import vanished_snapshot_entities

    snapshot = {"sensor.a", "sensor.gone", "sensor.b"}
    live = ["sensor.a", "sensor.b", "sensor.extra_not_in_snapshot"]
    # Only snapshot ids missing live are reported; live-only ids are not the gate's business.
    assert vanished_snapshot_entities(snapshot, live) == ["sensor.gone"]
    assert vanished_snapshot_entities(snapshot, list(snapshot)) == []


def test_verify_entities_snapshot_path_exists():
    """Same pinning as the automations gate — the snapshot must be readable and parseable."""
    from probe_ha import EXTERNAL_ENTITIES_YAML, snapshot_entity_ids

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
    from probe_ha import (
        AUTOMATIONS_DIR,
        automations_source_text,
        expected_automation_ids,
    )

    assert os.path.isdir(AUTOMATIONS_DIR), f"{AUTOMATIONS_DIR} is not a directory"
    ids = expected_automation_ids(automations_source_text())
    assert len(ids) > 1, "no automation ids parsed from the git-managed source"


def test_automations_source_text_rejects_empty_dir(tmp_path):
    # An empty directory must not read as "expect nothing": that gate passes on anything.
    from probe_ha import automations_source_text

    with pytest.raises(FileNotFoundError, match="no \\*\\.yaml"):
        automations_source_text(str(tmp_path))


def test_automations_source_text_concatenates_every_file(tmp_path):
    from probe_ha import automations_source_text, expected_automation_ids

    (tmp_path / "b.yaml").write_text("- id: two\n")
    (tmp_path / "a.yaml").write_text("- id: one\n")
    (tmp_path / "notes.txt").write_text("- id: ignored\n")
    assert expected_automation_ids(automations_source_text(str(tmp_path))) == {
        "one",
        "two",
    }
