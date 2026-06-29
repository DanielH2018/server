"""Tests for scripts/validate_grafana_dashboards.py — the datasource-uid guard."""
import json

from validate_grafana_dashboards import (
    DASHBOARDS_DIR,
    DATASOURCES_TEMPLATE,
    datasource_refs_in,
    provisioned_datasource_ids,
    validate,
)

# A minimal datasources.yml.j2 stand-in: the two real provisioned datasources.
_DATASOURCES = """\
apiVersion: 1
datasources:
  - name: Prometheus
    uid: EGdsQqhVk
    type: prometheus
  - name: loki
    uid: bf4q19tuivta8e
    type: loki
"""


def _write_datasources(tmp_path):
    p = tmp_path / "datasources.yml.j2"
    p.write_text(_DATASOURCES)
    return p


def _write_dashboard(dashboards_dir, name, obj):
    dashboards_dir.mkdir(parents=True, exist_ok=True)
    (dashboards_dir / name).write_text(json.dumps(obj))


def test_provisioned_ids_parses_uids_and_names(tmp_path):
    ids = provisioned_datasource_ids(_write_datasources(tmp_path))
    assert {"EGdsQqhVk", "bf4q19tuivta8e", "Prometheus", "loki"} <= ids


def test_object_form_uid_resolves_clean(tmp_path):
    dd = tmp_path / "dashboards"
    _write_dashboard(dd, "ok.json", {
        "uid": "my-board", "title": "OK",
        "panels": [{"title": "P", "datasource": {"type": "prometheus", "uid": "EGdsQqhVk"},
                    "targets": [{"datasource": {"type": "prometheus", "uid": "EGdsQqhVk"}}]}],
    })
    assert validate(dd, _write_datasources(tmp_path)) == []


def test_unknown_uid_flagged(tmp_path):
    dd = tmp_path / "dashboards"
    _write_dashboard(dd, "bad.json", {
        "uid": "my-board", "title": "Bad",
        "panels": [{"title": "Broken Panel",
                    "datasource": {"type": "prometheus", "uid": "IH0jqv6nz"}}],
    })
    errors = validate(dd, _write_datasources(tmp_path))
    assert len(errors) == 1
    assert "IH0jqv6nz" in errors[0]
    assert "Broken Panel" in errors[0]  # nearest-panel-title context


def test_legacy_string_form_handled(tmp_path):
    dd = tmp_path / "dashboards"
    _write_dashboard(dd, "legacy.json", {
        "uid": "b", "title": "Legacy",
        "panels": [{"title": "Good", "datasource": "EGdsQqhVk"},
                   {"title": "Stale", "datasource": "nope-uid"}],
    })
    errors = validate(dd, _write_datasources(tmp_path))
    assert len(errors) == 1
    assert "nope-uid" in errors[0]


def test_builtin_datasources_ignored(tmp_path):
    dd = tmp_path / "dashboards"
    _write_dashboard(dd, "builtins.json", {
        "uid": "b", "title": "Builtins",
        "panels": [
            {"title": "Anno", "datasource": {"type": "datasource", "uid": "-- Grafana --"}},
            {"title": "Mixed", "datasource": {"type": "datasource", "uid": "-- Mixed --"}},
            {"title": "Dash", "datasource": {"type": "datasource", "uid": "-- Dashboard --"}},
            {"title": "G", "datasource": {"type": "grafana", "uid": "grafana"}},
        ],
    })
    assert validate(dd, _write_datasources(tmp_path)) == []


def test_dashboard_own_uid_never_flagged(tmp_path):
    # The dashboard's own top-level uid is NOT a datasource ref and must never be flagged,
    # even when it is not a provisioned datasource uid (the sadlil-loki-apps-dashboard case).
    dd = tmp_path / "dashboards"
    _write_dashboard(dd, "ownuid.json", {
        "uid": "sadlil-loki-apps-dashboard", "title": "Own uid",
        "panels": [{"title": "P", "datasource": {"type": "loki", "uid": "bf4q19tuivta8e"}}],
    })
    assert validate(dd, _write_datasources(tmp_path)) == []


def test_invalid_json_reported(tmp_path):
    dd = tmp_path / "dashboards"
    dd.mkdir(parents=True)
    (dd / "broken.json").write_text("{not valid json")
    errors = validate(dd, _write_datasources(tmp_path))
    assert len(errors) == 1 and "broken.json" in errors[0]


def test_refs_collects_object_and_string_forms():
    refs = datasource_refs_in({
        "panels": [{"title": "T", "datasource": {"uid": "X"},
                    "targets": [{"datasource": "Y"}]}]
    })
    pairs = set(refs)
    assert ("X", "T") in pairs
    assert ("Y", "T") in pairs


def test_real_role_passes():
    # Regression guard: every existing provisioned board (and the new one, once added)
    # resolves against the real datasources template. The headline check.
    assert validate(DASHBOARDS_DIR, DATASOURCES_TEMPLATE) == []
