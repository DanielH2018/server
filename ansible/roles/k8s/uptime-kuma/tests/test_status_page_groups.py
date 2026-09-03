"""The committed group rules must place every committed monitor declaration.

These read the two sources that have to agree — `kuma_status_page_groups` in defaults and the
declarations in static-monitors.yaml.j2 — so a monitor added without a home fails here rather
than landing in the runtime catch-all where nobody looks.
"""

import json
import re
import sys as _sys
from pathlib import Path as _Path

import yaml

ROLE = _Path(__file__).resolve().parents[1]
_sys.path.insert(0, str(ROLE / "files"))

from render_status_page import bucket  # noqa: E402

DECLARATION = re.compile(r"^  (?P<id>[A-Za-z0-9._-]+)\.json: \|$")
NAME = re.compile(r'"name": "(?P<name>[^"]*)"')
TYPE = re.compile(r'"type": "(?P<type>[a-z_]+)"')

# The census this file walks is a regex over a template, so it returns an empty set the moment
# the template's shape changes — and every assertion below would then pass over nothing. These
# are monitors from four different groups; a rename that drops one is a real change to review,
# not an accident of parsing.
KNOWN_IDS = frozenset(
    {
        "daniel-pi-host",
        "grafana-k8s",
        "longhorn-backup",
        "monitor-bridge-b2-storage",
        "gitops-deploy-alive",
        "secret-rotation",
        "k3s-container-restarts",
        "status-page-sync",
    }
)


def declarations():
    """AutoKuma id -> (display name, entity type) for every monitor in the template."""
    text = (ROLE / "templates" / "static-monitors.yaml.j2").read_text()
    lines = text.splitlines()
    found = {}
    for position, line in enumerate(lines):
        match = DECLARATION.match(line)
        if not match:
            continue
        body = "\n".join(lines[position + 1 : position + 4])
        name = NAME.search(body)
        entity_type = TYPE.search(body)
        if name is None or entity_type is None:
            continue
        found[match.group("id")] = (name.group("name"), entity_type.group("type"))
    return found


def monitor_index():
    return {
        autokuma_id: name
        for autokuma_id, (name, entity_type) in declarations().items()
        if entity_type != "notification"
    }


def rules():
    return yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())[
        "kuma_status_page_groups"
    ]


def test_the_declaration_census_finds_every_known_monitor():
    index = monitor_index()
    assert KNOWN_IDS <= set(index), f"census lost: {sorted(KNOWN_IDS - set(index))}"
    assert len(index) >= 90, f"census found only {len(index)} monitors"


def test_every_declared_monitor_lands_in_a_named_group():
    index = monitor_index()
    placed = {
        autokuma_id
        for name, ids in bucket(index, rules())
        for autokuma_id in ids
        if name != "Other"
    }
    assert placed == set(index), f"unplaced: {sorted(set(index) - placed)}"


def test_the_catch_all_group_is_empty_for_committed_declarations():
    groups = dict(bucket(monitor_index(), rules()))
    assert groups["Other"] == []


def test_an_unplaced_monitor_reaches_the_catch_all_rather_than_erroring():
    groups = dict(bucket({"nothing-matches-this-xyz": "Nothing"}, rules()))
    assert groups["Other"] == ["nothing-matches-this-xyz"]


def test_display_names_are_unique():
    names = list(monitor_index().values())
    assert len(names) == len(set(names)), (
        "display name is the join key the sync resolves through"
    )


def test_no_status_page_is_declared_as_an_autokuma_entity():
    text = (ROLE / "templates" / "static-monitors.yaml.j2").read_text()
    assert '"status_page"' not in text, (
        "AutoKuma creates a status page and never edits one (sync.rs has no StatusPage update "
        "arm), and on_delete=delete would delete the live page if the declaration ever went away"
    )


def test_the_rendered_configmap_rules_parse_as_the_script_reads_them():
    """The ConfigMap ships these rules as JSON; a non-serialisable rule would fail at run time."""
    for rule in rules():
        json.loads(json.dumps(rule))
        assert isinstance(rule["name"], str)
        assert rule["match"] and all(
            isinstance(pattern, str) for pattern in rule["match"]
        )
        for pattern in rule["match"]:
            re.compile(pattern)
