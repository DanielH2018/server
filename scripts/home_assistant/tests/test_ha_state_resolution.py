"""Hermetic tests for the entity and service resolution checks (no live HA)."""

from home_assistant import ha_state_checks as hsm


def test_referenced_entities_collects_write_and_trigger_targets():
    config = {
        "automation": [
            {
                "id": "a",
                "alias": "A",
                "trigger": [
                    {
                        "platform": "state",
                        "entity_id": "binary_sensor.aqara_fp300_presence",
                    }
                ],
                "condition": [
                    {
                        "condition": "state",
                        "entity_id": "person.daniel",
                        "state": "home",
                    }
                ],
                "action": [
                    {
                        "service": "light.turn_on",
                        "target": {"entity_id": "light.bedroom_lights"},
                    }
                ],
            }
        ],
        "script": {},
        "scene": [],
    }
    refs = hsm.referenced_entities(config)
    assert {
        "binary_sensor.aqara_fp300_presence",
        "person.daniel",
        "light.bedroom_lights",
    } <= refs


def test_resolution_errors_flags_unknown_managed_entity():
    config = {
        "automation": [
            {
                "id": "a",
                "alias": "A",
                "action": [
                    {
                        "service": "switch.turn_on",
                        "target": {"entity_id": "switch.typo_does_not_exist"},
                    }
                ],
            }
        ],
        "script": {},
        "scene": [],
    }
    known = {"light.bedroom_lights"}  # switch.typo... absent
    errs = hsm.resolution_errors(config, known)
    assert any("switch.typo_does_not_exist" in e for e in errs)


def test_resolution_ignores_unmanaged_domains_and_templated():
    config = {
        "automation": [
            {
                "id": "a",
                "alias": "A",
                "action": [
                    {"service": "notify.mobile_app_x", "data": {"message": "hi"}},
                    {"service": "light.turn_on", "target": {"entity_id": "{{ x }}"}},
                ],
            }
        ],
        "script": {},
        "scene": [],
    }
    assert hsm.resolution_errors(config, set()) == []


def _automation_reading(template: str, target: str = "automation.go_dark") -> dict:
    return {
        "automation": [
            {"id": "dismiss", "alias": "Go dark", "action": []},
            {
                "id": "show",
                "alias": "Show",
                "condition": [{"condition": "template", "value_template": template}],
                "action": [
                    {"service": "automation.trigger", "target": {"entity_id": target}}
                ],
            },
        ],
        "script": {},
        "scene": [],
    }


def test_resolution_errors_flags_an_automation_read_by_its_id():
    config = _automation_reading(
        "{{ state_attr('automation.dismiss', 'last_triggered') }} "
        "{{ is_state('automation.go_dark', 'on') }}"
    )
    errs = hsm.resolution_errors(config, set())
    assert errs == [
        "unresolved entity reference: automation.dismiss "
        "(typo, or run `ha_state_model.py refresh` if it is a new device)"
    ]


def test_resolution_errors_flags_an_unknown_automation_entity_id_field():
    config = _automation_reading("{{ true }}", target="automation.dismiss")
    assert any("automation.dismiss" in e for e in hsm.resolution_errors(config, set()))


def test_referenced_services_collects_literals_skips_templated():
    config = {
        "automation": [
            {
                "id": "a",
                "alias": "A",
                "action": [
                    {"service": "notify.mobile_app_x"},
                    {"service": "{{ 'light.' ~ 'turn_on' }}"},
                    {"service": "scene.turn_on", "target": {"entity_id": "scene.x"}},
                ],
            }
        ],
        "script": {},
    }
    assert hsm.referenced_services(config) == {"notify.mobile_app_x", "scene.turn_on"}


def test_service_resolution_flags_unknown_in_any_domain():
    # notify is NOT a managed entity-domain, but a typo'd notify SERVICE must still be caught
    # (the documented notify.pixel_watch_3 service_not_found bug).
    config = {
        "automation": [
            {"id": "a", "alias": "A", "action": [{"service": "notify.pixel_watch_3"}]}
        ],
        "script": {},
    }
    known = {"notify.mobile_app_pixel_watch_3", "light.turn_on"}
    errs = hsm.service_resolution_errors(config, known)
    assert any("notify.pixel_watch_3" in e for e in errs)


def test_service_resolution_resolves_config_script_via_known():
    config = {
        "automation": [
            {"id": "a", "alias": "A", "action": [{"service": "script.bedroom_blip"}]}
        ],
        "script": {"bedroom_blip": {"sequence": []}},
    }
    known = hsm.config_services(
        config
    )  # the freshness hatch resolves a brand-new script
    assert hsm.service_resolution_errors(config, known) == []
