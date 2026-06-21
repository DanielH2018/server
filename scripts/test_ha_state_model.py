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
