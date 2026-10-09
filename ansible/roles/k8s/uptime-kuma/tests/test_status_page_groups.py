"""The committed group rules must place every committed monitor declaration.

These read the two sources that have to agree — `uptime_kuma_k8s_status_page_groups` in defaults and the
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

from _k8s_render import render_role_template  # noqa: E402
from kuma_monitors import kuma_ingress_monitors  # noqa: E402
from py_table import py_table  # noqa: E402
from render_status_page import bucket  # noqa: E402

# monitor-bridge's push tiles render from one loop over this table (#3781), so their ids are
# the rows' `kuma_id`s rather than literal keys in the template.
CHECK_TABLE = ROLE.parent / "monitor-bridge" / "files" / "check_table.py"
# The ingress tiles render from one loop over this host's containers_list (#3690).
HOST_VARS = ROLE.parents[2] / "inventory" / "host_vars" / "daniel-box.yml"

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
    """AutoKuma id -> (display name, entity type) for every monitor in the template.

    A literal declaration is read off the template's lines, the bridge tile loop off the table
    it iterates, and the ingress tile loop off the containers_list it iterates.
    """
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
    if "py_table('CHECKS')" in text:
        for row in py_table(CHECK_TABLE.read_text(), "CHECKS"):
            found[row["kuma_id"]] = (row["display"], "push")
    if "kuma_ingress_monitors" in text:
        entries = yaml.safe_load(HOST_VARS.read_text())["containers_list"]
        for tile in kuma_ingress_monitors(entries):
            found[tile["id"]] = (tile["name"], "http")
    return found


def monitor_index():
    return {
        autokuma_id: name
        for autokuma_id, (name, entity_type) in declarations().items()
        if entity_type not in ("notification", "tag")
    }


def rules():
    """The rules as the sync ConfigMap ships them, with the bridge tiles pinned in (#3781)."""
    configmap = yaml.safe_load(
        render_role_template("uptime-kuma", "status-page-sync-configmap.yaml.j2")
    )
    return json.loads(configmap["data"]["rules.json"])


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


def test_every_bridge_tile_lands_in_the_group_its_row_names():
    """A broader pattern in an earlier group would take a pinned tile first; this says so."""
    placed = {i: name for name, ids in bucket(monitor_index(), rules()) for i in ids}
    wanted = {
        row["kuma_id"]: row["status_group"]
        for row in py_table(CHECK_TABLE.read_text(), "CHECKS")
    }
    assert wanted["monitor-bridge-traefik-421"] == "Observability"
    assert {i: placed.get(i) for i in wanted} == wanted


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
