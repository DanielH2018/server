"""Hermetic tests for the HA state-model extractor + checks (no live HA / Docker / network)."""
import ha_state_model as hsm


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
