"""The recorder excludes automations and scripts by entity, never by domain (#8385).

The recorder drops every event whose data.entity_id an exclude matches, so a domain exclude also
drops each automation_triggered and script_started event, and the logbook loses which automation
changed a light. An excluded entity id that no longer exists excludes nothing.
"""

from pathlib import Path

import yaml

ROLE = Path(__file__).resolve().parent.parent


def _recorder_exclude() -> dict:
    class _Loader(yaml.SafeLoader):
        pass

    _Loader.add_multi_constructor("!", lambda loader, suffix, node: None)
    config = yaml.load(
        (ROLE / "files" / "configuration.yaml").read_text(), Loader=_Loader
    )
    return config["recorder"]["exclude"]


def test_no_automation_or_script_domain_exclude():
    domains = _recorder_exclude().get("domains") or []
    assert "automation" not in domains
    assert "script" not in domains


def test_excluded_automations_and_scripts_exist():
    known = set(
        yaml.safe_load((ROLE / "state" / "external_entities.yml").read_text())[
            "entities"
        ]
    )
    excluded = [
        e
        for e in _recorder_exclude()["entities"]
        if e.split(".")[0] in ("automation", "script")
    ]
    assert "automation.ha_heartbeat" in excluded
    assert set(excluded) <= known
