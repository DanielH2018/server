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
