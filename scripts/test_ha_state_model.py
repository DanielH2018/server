"""Hermetic tests for the HA state-model extractor + checks (no live HA / Docker / network)."""
import ha_state_model as hsm
import validate_ha_config as vhc


def test_call_service_handles_service_and_action_keys():
    assert hsm.call_service({"service": "light.turn_on"}) == "light.turn_on"
    assert hsm.call_service({"action": "fan.set_percentage"}) == "fan.set_percentage"
    assert hsm.call_service({"condition": "state"}) is None


def test_call_targets_scalar_list_and_legacy_forms():
    assert hsm.call_targets({"service": "x.y", "target": {"entity_id": "light.a"}}) == ["light.a"]
    assert hsm.call_targets(
        {"service": "x.y", "target": {"entity_id": ["light.a", "light.b"]}}
    ) == ["light.a", "light.b"]
    # legacy top-level + data.entity_id forms
    assert hsm.call_targets({"service": "x.y", "entity_id": "switch.a"}) == ["switch.a"]
    assert hsm.call_targets({"service": "x.y", "data": {"entity_id": "scene.a"}}) == ["scene.a"]


def test_call_targets_keeps_templated_ids_verbatim():
    assert hsm.call_targets(
        {"service": "x.y", "target": {"entity_id": "{{ repeat.item }}"}}
    ) == ["{{ repeat.item }}"]


def test_iter_service_calls_recurses_choose_if_repeat():
    action = [
        {"choose": [
            {"conditions": [{"condition": "state"}],
             "sequence": [{"service": "input_boolean.turn_on",
                           "target": {"entity_id": "input_boolean.x"}}]}],
         "default": [
            {"if": [{"condition": "state"}],
             "then": [{"service": "timer.start", "target": {"entity_id": "timer.t"}}],
             "else": [{"repeat": {"sequence": [
                 {"service": "light.turn_off", "target": {"entity_id": "light.l"}}]}}]}]},
    ]
    svcs = {hsm.call_service(c) for c in hsm.iter_service_calls(action)}
    assert svcs == {"input_boolean.turn_on", "timer.start", "light.turn_off"}


def test_slugify_matches_ha_basic_rules():
    assert hsm.slugify("Bedroom Tap Dial control") == "bedroom_tap_dial_control"
    assert hsm.slugify("UPS power event!") == "ups_power_event"


SCENES = [
    {"id": "bedroom_nightlight", "name": "Bedroom Nightlight",
     "entities": {"light.bedroom_lights": {"state": "on"}}},
]


def test_scene_entity_map():
    m = hsm.scene_entity_map(SCENES)
    assert m == {"scene.bedroom_nightlight": ["light.bedroom_lights"]}


def test_automation_writer_uses_alias_slug():
    assert hsm.automation_writer({"id": "x", "alias": "Bedroom away"}) == "automation.bedroom_away"
    assert hsm.automation_writer({"id": "ups_power_event"}) == "automation.ups_power_event"


def test_extract_writes_attributes_and_resolves_scenes():
    autos = [
        {"id": "a", "alias": "Bedroom away", "action": [
            {"service": "light.turn_off", "target": {"entity_id": "light.bedroom_lights"}},
            {"service": "scene.turn_on", "target": {"entity_id": "scene.bedroom_nightlight"}}]},
    ]
    scripts = {
        "bedroom_bedtime": {"sequence": [
            {"service": "input_boolean.turn_on",
             "target": {"entity_id": "input_boolean.bedroom_sleep_mode"}},
            {"service": "light.turn_on",
             "target": {"entity_id": "light.bedroom_lights"}},
            {"service": "light.turn_on",
             "target": {"entity_id": "{{ some_var }}"}}]},
    }
    writes, dynamic = hsm.extract_writes(autos, scripts, hsm.scene_entity_map(SCENES))
    # scene.turn_on resolved to the light; direct light.turn_off also attributed
    assert writes["light.bedroom_lights"] == ["automation.bedroom_away", "script.bedroom_bedtime"]
    assert writes["input_boolean.bedroom_sleep_mode"] == ["script.bedroom_bedtime"]
    assert dynamic["script.bedroom_bedtime"] == ["{{ some_var }}"]
    # the scene entity itself is not recorded as a written entity
    assert "scene.bedroom_nightlight" not in writes


CONFIG = {
    "input_boolean": {"bedroom_manual_off": {"name": "Bedroom manual off override"}},
    "input_number": {"bedroom_fan_expected_level": {"name": "Bedroom fan expected level"}},
    "timer": {"bedroom_fan_dial": {"name": "Bedroom fan-dial mode"}},
    "binary_sensor": [
        {"platform": "threshold", "name": "Bedroom CO2 high",
         "entity_id": "sensor.bedroom_airgradient_one_carbon_dioxide", "upper": 1000},
        {"platform": "threshold", "name": "Bedroom FP300 battery low",
         "entity_id": "sensor.aqara_fp300_battery", "lower": 20},
    ],
}


def test_extract_cells():
    cells = hsm.extract_cells(CONFIG)
    assert cells["bedroom_manual_off"]["entity"] == "input_boolean.bedroom_manual_off"
    assert cells["bedroom_fan_dial"]["entity"] == "timer.bedroom_fan_dial"
    assert cells["bedroom_fan_expected_level"]["domain"] == "input_number"


def test_extract_thresholds_records_bound_direction():
    th = {t["entity"]: t for t in hsm.extract_thresholds(CONFIG)}
    assert th["binary_sensor.bedroom_co2_high"]["bound"] == "upper"
    assert th["binary_sensor.bedroom_fp300_battery_low"]["bound"] == "lower"


def test_config_entities_includes_helpers_scenes_thresholds():
    ents = hsm.config_entities(CONFIG, SCENES)
    assert "input_boolean.bedroom_manual_off" in ents
    assert "timer.bedroom_fan_dial" in ents
    assert "binary_sensor.bedroom_co2_high" in ents
    assert "scene.bedroom_nightlight" in ents


def test_config_entities_includes_runtime_created_scenes():
    # scene.create builds scene.bedroom_pre_alert at runtime; a later scene.turn_on references it.
    config = {"script": {"alert": {"sequence": [
        {"service": "scene.create",
         "data": {"scene_id": "bedroom_pre_alert", "snapshot_entities": ["light.bedroom_lights"]}}]}}}
    assert "scene.bedroom_pre_alert" in hsm.config_entities(config, [])


def test_load_role_returns_real_automation_list():
    config = hsm.load_role()
    aliases = {a.get("alias") for a in config.get("automation", [])}
    assert "Bedroom away" in aliases          # sanity: the real role loaded
    assert isinstance(config.get("script"), dict)


def test_build_model_is_deterministic_and_sorted():
    config = {**CONFIG, "automation": [
        {"id": "a", "alias": "Bedroom away", "action": [
            {"service": "light.turn_off", "target": {"entity_id": "light.bedroom_lights"}}]}],
        "script": {}, "scene": SCENES}
    m1 = hsm.build_model(config)
    m2 = hsm.build_model(config)
    assert m1 == m2
    assert m1["writes"]["light.bedroom_lights"] == ["automation.bedroom_away"]
    assert "light.bedroom_lights" in m1["actuators"]


def test_render_derived_yaml_roundtrips():
    import yaml as y
    model = {"cells": {}, "actuators": ["light.bedroom_lights"],
             "writes": {"light.bedroom_lights": ["automation.x"]}, "dynamic_writes": {}}
    text = hsm.render_derived_yaml(model)
    assert y.safe_load(text)["writes"]["light.bedroom_lights"] == ["automation.x"]


def test_dump_yaml_indents_sequences_for_ansible_lint():
    # ansible-lint/yamllint `indent-sequences`: list items indented UNDER their key.
    out = hsm._dump_yaml({"writes": {"light.x": ["automation.a", "automation.b"]}})
    assert "\n    - automation.a" in out


def test_render_state_md_lists_actuator_writers():
    model = {"cells": {"bedroom_manual_off": {"entity": "input_boolean.bedroom_manual_off",
             "name": "Bedroom manual off override"}},
             "actuators": ["light.bedroom_lights"],
             "writes": {"light.bedroom_lights": ["automation.bedroom_away"]},
             "dynamic_writes": {}}
    md = hsm.render_state_md(model)
    assert "light.bedroom_lights" in md
    assert "automation.bedroom_away" in md


def test_referenced_entities_collects_write_and_trigger_targets():
    config = {"automation": [
        {"id": "a", "alias": "A", "trigger": [
            {"platform": "state", "entity_id": "binary_sensor.aqara_fp300_presence"}],
         "condition": [{"condition": "state", "entity_id": "person.daniel", "state": "home"}],
         "action": [{"service": "light.turn_on", "target": {"entity_id": "light.bedroom_lights"}}]}],
        "script": {}, "scene": []}
    refs = hsm.referenced_entities(config)
    assert {"binary_sensor.aqara_fp300_presence", "person.daniel", "light.bedroom_lights"} <= refs


def test_resolution_errors_flags_unknown_managed_entity():
    config = {"automation": [
        {"id": "a", "alias": "A", "action": [
            {"service": "switch.turn_on", "target": {"entity_id": "switch.typo_does_not_exist"}}]}],
        "script": {}, "scene": []}
    known = {"light.bedroom_lights"}  # switch.typo... absent
    errs = hsm.resolution_errors(config, known)
    assert any("switch.typo_does_not_exist" in e for e in errs)


def test_resolution_ignores_unmanaged_domains_and_templated():
    config = {"automation": [
        {"id": "a", "alias": "A", "action": [
            {"service": "notify.mobile_app_x", "data": {"message": "hi"}},
            {"service": "light.turn_on", "target": {"entity_id": "{{ x }}"}}]}],
        "script": {}, "scene": []}
    assert hsm.resolution_errors(config, set()) == []


def test_override_writer_errors_flags_undeclared_writer():
    writes = {"input_boolean.bedroom_sleep_mode": ["script.bedroom_bedtime", "automation.new_thing"]}
    expected = {"input_boolean.bedroom_sleep_mode": ["script.bedroom_bedtime"]}
    errs = hsm.override_writer_errors(writes, expected)
    assert any("automation.new_thing" in e for e in errs)


def test_override_writer_errors_clean_when_match():
    writes = {"input_boolean.bedroom_sleep_mode": ["script.bedroom_bedtime"]}
    expected = {"input_boolean.bedroom_sleep_mode": ["script.bedroom_bedtime"]}
    assert hsm.override_writer_errors(writes, expected) == []


# The real bedroom_threshold_alert groups each category's sensors into ONE bad + ONE ok trigger
# with a LIST entity_id (verified against files/automations.yaml), so the tests model lists.
def _threshold_config(declared, triggers):
    return {"binary_sensor": declared, "script": {},
            "automation": [{"id": "bedroom_threshold_alert", "alias": "Bedroom threshold alert",
                            "trigger": triggers, "action": []}]}

_CO2 = {"platform": "threshold", "name": "Bedroom CO2 high", "entity_id": "sensor.x", "upper": 1}
_VOC = {"platform": "threshold", "name": "Bedroom VOC high", "entity_id": "sensor.y", "upper": 1}
_AQ_PAIR = [
    {"platform": "state", "entity_id": ["binary_sensor.bedroom_co2_high"],
     "to": "on", "id": "airquality_bad"},
    {"platform": "state", "entity_id": ["binary_sensor.bedroom_co2_high"],
     "to": "off", "id": "airquality_ok"}]


def test_threshold_pairing_clean_when_wired_both_directions():
    assert hsm.threshold_pairing_errors(_threshold_config([_CO2], _AQ_PAIR)) == []


def test_threshold_pairing_flags_declared_but_unwired_sensor():
    # VOC declared but not added to any trigger list -> flagged (the half-added-metric case).
    errs = hsm.threshold_pairing_errors(_threshold_config([_CO2, _VOC], _AQ_PAIR))
    assert any("bedroom_voc_high" in e for e in errs)


def test_threshold_pairing_flags_missing_ok_category():
    # A category with a _bad trigger but no _ok (the half-added-category case).
    triggers = [{"platform": "state", "entity_id": ["binary_sensor.bedroom_co2_high"],
                 "to": "on", "id": "airquality_bad"}]
    errs = hsm.threshold_pairing_errors(_threshold_config([_CO2], triggers))
    assert any("missing its _ok" in e for e in errs)


def test_threshold_pairing_flags_one_direction_only():
    # CO2 wired in the _bad (on) list but absent from the _ok (off) list -> direction gap.
    triggers = [
        {"platform": "state", "entity_id": ["binary_sensor.bedroom_co2_high"],
         "to": "on", "id": "airquality_bad"},
        {"platform": "state", "entity_id": [], "to": "off", "id": "airquality_ok"}]
    errs = hsm.threshold_pairing_errors(_threshold_config([_CO2], triggers))
    assert any("off trigger direction" in e for e in errs)


def test_alias_collision_flags_duplicate_slug():
    config = {"automation": [{"id": "a", "alias": "Bedroom away"},
                             {"id": "b", "alias": "Bedroom  away"}], "script": {}}
    assert hsm.alias_collision_errors(config) != []


def test_single_writer_report_lists_extra_writers():
    writes = {"light.bedroom_lights": ["script.bedroom_lights_set", "automation.bedroom_away"]}
    sanctioned = {"light.bedroom_lights": "script.bedroom_lights_set"}
    rep = hsm.single_writer_report(writes, sanctioned)
    assert any("automation.bedroom_away" in r for r in rep)


def test_freshness_errors_flag_stale_committed_file(tmp_path, monkeypatch):
    # point the artifact paths at a temp dir with deliberately-wrong content
    monkeypatch.setattr(hsm, "DERIVED_YAML", tmp_path / "derived_state.yml")
    monkeypatch.setattr(hsm, "STATE_MD", tmp_path / "STATE.md")
    (tmp_path / "derived_state.yml").write_text("stale: true\n")
    (tmp_path / "STATE.md").write_text("stale\n")
    errs = hsm.freshness_errors()
    assert any("derived_state.yml" in e for e in errs)


def test_check_errors_on_real_role_is_clean_after_generate(tmp_path):
    # After Task 4/5/6 produced fresh artifacts + snapshot, the real role must validate clean.
    errs = hsm.check_errors()
    assert errs == [], "real role failed state-model checks:\n" + "\n".join(errs)
