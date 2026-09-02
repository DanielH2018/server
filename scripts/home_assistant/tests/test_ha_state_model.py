"""Hermetic tests for the HA state-model extractor + checks (no live HA / Docker / network)."""

import yaml
import ha_state_model as hsm
import probe_ha


def test_call_service_handles_service_and_action_keys():
    assert hsm.call_service({"service": "light.turn_on"}) == "light.turn_on"
    assert hsm.call_service({"action": "fan.set_percentage"}) == "fan.set_percentage"
    assert hsm.call_service({"condition": "state"}) is None


def test_call_targets_scalar_list_and_legacy_forms():
    assert hsm.call_targets({"service": "x.y", "target": {"entity_id": "light.a"}}) == [
        "light.a"
    ]
    assert hsm.call_targets(
        {"service": "x.y", "target": {"entity_id": ["light.a", "light.b"]}}
    ) == ["light.a", "light.b"]
    # legacy top-level + data.entity_id forms
    assert hsm.call_targets({"service": "x.y", "entity_id": "switch.a"}) == ["switch.a"]
    assert hsm.call_targets({"service": "x.y", "data": {"entity_id": "scene.a"}}) == [
        "scene.a"
    ]


def test_call_targets_keeps_templated_ids_verbatim():
    assert hsm.call_targets(
        {"service": "x.y", "target": {"entity_id": "{{ repeat.item }}"}}
    ) == ["{{ repeat.item }}"]


def test_iter_service_calls_recurses_choose_if_repeat():
    action = [
        {
            "choose": [
                {
                    "conditions": [{"condition": "state"}],
                    "sequence": [
                        {
                            "service": "input_boolean.turn_on",
                            "target": {"entity_id": "input_boolean.x"},
                        }
                    ],
                }
            ],
            "default": [
                {
                    "if": [{"condition": "state"}],
                    "then": [
                        {"service": "timer.start", "target": {"entity_id": "timer.t"}}
                    ],
                    "else": [
                        {
                            "repeat": {
                                "sequence": [
                                    {
                                        "service": "light.turn_off",
                                        "target": {"entity_id": "light.l"},
                                    }
                                ]
                            }
                        }
                    ],
                }
            ],
        },
    ]
    svcs = {hsm.call_service(c) for c in hsm.iter_service_calls(action)}
    assert svcs == {"input_boolean.turn_on", "timer.start", "light.turn_off"}


def test_slugify_matches_ha_basic_rules():
    assert hsm.slugify("Bedroom Tap Dial control") == "bedroom_tap_dial_control"
    assert hsm.slugify("UPS power event!") == "ups_power_event"


SCENES = [
    {
        "id": "bedroom_nightlight",
        "name": "Bedroom Nightlight",
        "entities": {"light.bedroom_lights": {"state": "on"}},
    },
]


def test_scene_entity_map():
    m = hsm.scene_entity_map(SCENES)
    assert m == {"scene.bedroom_nightlight": ["light.bedroom_lights"]}


def test_automation_writer_uses_alias_slug():
    assert (
        hsm.automation_writer({"id": "x", "alias": "Bedroom away"})
        == "automation.bedroom_away"
    )
    assert (
        hsm.automation_writer({"id": "ups_power_event"}) == "automation.ups_power_event"
    )


def test_extract_writes_attributes_and_resolves_scenes():
    autos = [
        {
            "id": "a",
            "alias": "Bedroom away",
            "action": [
                {
                    "service": "light.turn_off",
                    "target": {"entity_id": "light.bedroom_lights"},
                },
                {
                    "service": "scene.turn_on",
                    "target": {"entity_id": "scene.bedroom_nightlight"},
                },
            ],
        },
    ]
    scripts = {
        "bedroom_bedtime": {
            "sequence": [
                {
                    "service": "input_boolean.turn_on",
                    "target": {"entity_id": "input_boolean.bedroom_sleep_mode"},
                },
                {
                    "service": "light.turn_on",
                    "target": {"entity_id": "light.bedroom_lights"},
                },
                {"service": "light.turn_on", "target": {"entity_id": "{{ some_var }}"}},
            ]
        },
    }
    writes, dynamic = hsm.extract_writes(autos, scripts, hsm.scene_entity_map(SCENES))
    # scene.turn_on resolved to the light; direct light.turn_off also attributed
    assert writes["light.bedroom_lights"] == [
        "automation.bedroom_away",
        "script.bedroom_bedtime",
    ]
    assert writes["input_boolean.bedroom_sleep_mode"] == ["script.bedroom_bedtime"]
    assert dynamic["script.bedroom_bedtime"] == ["{{ some_var }}"]
    # the scene entity itself is not recorded as a written entity
    assert "scene.bedroom_nightlight" not in writes


CONFIG = {
    "input_boolean": {"bedroom_manual_off": {"name": "Bedroom manual off override"}},
    "input_number": {
        "bedroom_fan_expected_level": {"name": "Bedroom fan expected level"}
    },
    "timer": {"bedroom_fan_dial": {"name": "Bedroom fan-dial mode"}},
    "binary_sensor": [
        {
            "platform": "threshold",
            "name": "Bedroom CO2 high",
            "entity_id": "sensor.bedroom_airgradient_one_carbon_dioxide",
            "upper": 1000,
        },
        {
            "platform": "threshold",
            "name": "Bedroom FP300 battery low",
            "entity_id": "sensor.aqara_fp300_battery",
            "lower": 20,
        },
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
    config = {
        "script": {
            "alert": {
                "sequence": [
                    {
                        "service": "scene.create",
                        "data": {
                            "scene_id": "bedroom_pre_alert",
                            "snapshot_entities": ["light.bedroom_lights"],
                        },
                    }
                ]
            }
        }
    }
    assert "scene.bedroom_pre_alert" in hsm.config_entities(config, [])


def test_load_role_returns_real_automation_list():
    config = hsm.load_role()
    aliases = {a.get("alias") for a in config.get("automation", [])}
    assert "Bedroom away" in aliases  # sanity: the real role loaded
    assert isinstance(config.get("script"), dict)


def test_build_model_is_deterministic_and_sorted():
    config = {
        **CONFIG,
        "automation": [
            {
                "id": "a",
                "alias": "Bedroom away",
                "action": [
                    {
                        "service": "light.turn_off",
                        "target": {"entity_id": "light.bedroom_lights"},
                    }
                ],
            }
        ],
        "script": {},
        "scene": SCENES,
    }
    m1 = hsm.build_model(config)
    m2 = hsm.build_model(config)
    assert m1 == m2
    assert m1["writes"]["light.bedroom_lights"] == ["automation.bedroom_away"]
    assert "light.bedroom_lights" in m1["actuators"]


def test_render_derived_yaml_roundtrips():
    import yaml as y

    model = {
        "cells": {},
        "actuators": ["light.bedroom_lights"],
        "writes": {"light.bedroom_lights": ["automation.x"]},
        "dynamic_writes": {},
    }
    text = hsm.render_derived_yaml(model)
    assert y.safe_load(text)["writes"]["light.bedroom_lights"] == ["automation.x"]


def test_dump_yaml_indents_sequences_for_ansible_lint():
    # ansible-lint/yamllint `indent-sequences`: list items indented UNDER their key.
    out = hsm._dump_yaml({"writes": {"light.x": ["automation.a", "automation.b"]}})
    assert "\n    - automation.a" in out


def test_render_state_md_lists_actuator_writers():
    model = {
        "cells": {
            "bedroom_manual_off": {
                "entity": "input_boolean.bedroom_manual_off",
                "name": "Bedroom manual off override",
            }
        },
        "actuators": ["light.bedroom_lights"],
        "writes": {"light.bedroom_lights": ["automation.bedroom_away"]},
        "dynamic_writes": {},
    }
    md = hsm.render_state_md(model)
    assert "light.bedroom_lights" in md
    assert "automation.bedroom_away" in md


def test_parse_services_flattens_domains():
    api = [
        {
            "domain": "notify",
            "services": {"mobile_app_x": {}, "persistent_notification": {}},
        },
        {"domain": "light", "services": {"turn_on": {}, "turn_off": {}}},
    ]
    assert hsm.parse_services(api) == {
        "notify.mobile_app_x",
        "notify.persistent_notification",
        "light.turn_on",
        "light.turn_off",
    }


def test_config_services_registers_each_script():
    config = {"script": {"bedroom_lights_set": {}, "bedroom_blip": {}}}
    assert hsm.config_services(config) == {
        "script.bedroom_lights_set",
        "script.bedroom_blip",
    }


def test_cmd_refresh_writes_both_snapshots(tmp_path, monkeypatch):
    monkeypatch.setattr(hsm, "STATE_DIR", tmp_path)
    monkeypatch.setattr(hsm, "EXTERNAL_YAML", tmp_path / "external_entities.yml")
    monkeypatch.setattr(
        hsm, "EXTERNAL_SERVICES_YAML", tmp_path / "external_services.yml"
    )
    rc = hsm.cmd_refresh(
        get_states=lambda: ["light.bedroom_lights", "sensor.outdoor_pm2_5"],
        get_services=lambda: {"notify.mobile_app_pixel_watch_3", "light.turn_on"},
    )
    assert rc == 0
    saved = yaml.safe_load((tmp_path / "external_services.yml").read_text())
    assert "notify.mobile_app_pixel_watch_3" in saved["services"]


def test_ha_state_rows_renders_cell_values_and_anomaly():
    model = {
        "cells": {
            "bedroom_sleep_mode": {
                "entity": "input_boolean.bedroom_sleep_mode",
                "name": "Bedroom sleep mode",
            }
        },
        "actuators": [],
        "writes": {},
        "dynamic_writes": {},
    }
    states = [
        {
            "entity_id": "input_boolean.bedroom_sleep_mode",
            "state": "on",
            "last_changed": "2026-06-21T12:00:00+00:00",
        }
    ]
    out = probe_ha.ha_state_rows(states, model)
    assert "input_boolean.bedroom_sleep_mode" in out
    assert "on" in out


def test_cmd_refresh_live_imports_resolve_under_direct_invocation():
    """`refresh`'s live branch must import cleanly with ONLY the script's own directory on
    sys.path — the layout a direct `python scripts/home_assistant/ha_state_model.py` gets,
    and the one the cron and the prek hook actually run under.

    This runs in a subprocess with PYTHONPATH cleared on purpose. pyproject's `pythonpath`
    is a pytest setting, so an in-process import resolves names a direct invocation cannot
    see — which is how this one path broke three times behind a green suite.
    `test_cmd_refresh_writes_both_snapshots` injects get_states/get_services and is
    structurally upstream of the import, so it stays green either way.

    Red-proof: delete the `scripts/diagnostics` sys.path insert in ha_state_model.py and
    this fails with `ModuleNotFoundError: No module named 'probe_core'`.
    """
    import os
    import subprocess
    import sys
    from pathlib import Path

    script_dir = Path(hsm.__file__).resolve().parent
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); import ha_state_model; "
            "from diagnostics import probe_core, probe_ha; "
            "print(probe_core.__name__, probe_ha.__name__)",
            str(script_dir),
        ],
        cwd=str(script_dir.parent.parent),
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "diagnostics.probe_ha" in proc.stdout
