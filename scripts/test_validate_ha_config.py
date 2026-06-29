"""Tests for scripts/validate_ha_config.py — the lightweight HA config validator."""

import yaml
import pytest

from validate_ha_config import (
    HAConfigError,
    HAConfigLoader,
    ROLE_DIR,
    assemble_config,
    jinja_errors,
    load_config,
    validate,
)


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _load(path):
    with open(path) as f:
        return yaml.load(f, Loader=HAConfigLoader)


def test_loader_detects_duplicate_keys(tmp_path):
    p = tmp_path / "dup.yaml"
    p.write_text("recorder:\n  purge_keep_days: 5\nrecorder:\n  purge_keep_days: 7\n")
    with pytest.raises(HAConfigError, match="duplicate key"):
        _load(p)


def test_loader_resolves_include(tmp_path):
    _write(tmp_path / "b.yaml", "- one\n- two\n")
    _write(tmp_path / "a.yaml", "items: !include b.yaml\n")
    assert _load(tmp_path / "a.yaml") == {"items": ["one", "two"]}


def test_loader_missing_include_raises(tmp_path):
    _write(tmp_path / "a.yaml", "x: !include nope.yaml\n")
    with pytest.raises(HAConfigError, match="not found"):
        _load(tmp_path / "a.yaml")


def test_loader_secret_is_placeholder(tmp_path):
    _write(tmp_path / "a.yaml", "token: !secret my_token\n")
    assert _load(tmp_path / "a.yaml") == {"token": "<secret>"}


def test_loader_malformed_yaml_raises(tmp_path):
    _write(tmp_path / "bad.yaml", "foo: [1, 2\n")  # unclosed flow sequence
    with pytest.raises(yaml.YAMLError):
        _load(tmp_path / "bad.yaml")


def test_assemble_rejects_ansible_markers(tmp_path):
    role = tmp_path / "role"
    _write(
        role / "templates/configuration.yaml.j2",
        "homeassistant:\n  name: {{ ha_name }}\n",
    )
    _write(role / "templates/customize.yaml.j2", "{}\n")
    _write(role / "templates/ui-lovelace.yaml.j2", "{}\n")
    with pytest.raises(HAConfigError, match="Ansible templating"):
        assemble_config(role, tmp_path / "dest")


def test_real_config_structural_clean(tmp_path):
    assemble_config(ROLE_DIR, tmp_path)
    errors, trees = load_config(tmp_path)
    assert errors == [], errors
    assert len(trees) == 2  # configuration.yaml + ui-lovelace.yaml both loaded


def test_loader_supports_merge_keys(tmp_path):
    # A legal YAML merge-override must NOT be mis-flagged as a duplicate key.
    p = tmp_path / "m.yaml"
    p.write_text("base: &b\n  x: 1\nderived:\n  <<: *b\n  y: 2\n")
    assert _load(p)["derived"] == {"x": 1, "y": 2}


def test_loader_rejects_circular_include(tmp_path):
    _write(tmp_path / "a.yaml", "x: !include b.yaml\n")
    _write(tmp_path / "b.yaml", "y: !include a.yaml\n")
    with pytest.raises(HAConfigError, match="circular"):
        _load(tmp_path / "a.yaml")


def test_loader_detects_duplicate_inside_include(tmp_path):
    _write(tmp_path / "b.yaml", "k: 1\nk: 2\n")
    _write(tmp_path / "a.yaml", "data: !include b.yaml\n")
    with pytest.raises(HAConfigError, match="duplicate key"):
        _load(tmp_path / "a.yaml")


def test_loader_detects_duplicate_in_list_item(tmp_path):
    p = tmp_path / "list.yaml"
    p.write_text("- name: one\n  name: two\n")
    with pytest.raises(HAConfigError, match="duplicate key"):
        _load(p)


def test_jinja_errors_flags_unclosed_inline_block(tmp_path):
    cdir = tmp_path / "ct"
    cdir.mkdir()
    trees = [{"automation": [{"value_template": "{% if x %}no end"}]}]
    errors = jinja_errors(trees, cdir)
    assert errors and "Jinja syntax error" in errors[0]


def test_jinja_errors_flags_bad_macro_file(tmp_path):
    cdir = tmp_path / "ct"
    cdir.mkdir()
    (cdir / "broken.jinja").write_text("{% macro f(x) %}{{ x }}\n")  # missing endmacro
    errors = jinja_errors([], cdir)
    assert any("broken.jinja" in e for e in errors)


def test_jinja_errors_clean_on_valid(tmp_path):
    cdir = tmp_path / "ct"
    cdir.mkdir()
    (cdir / "ok.jinja").write_text("{% macro f(x) %}{{ x | float(0) }}{% endmacro %}\n")
    trees = [{"value_template": "{{ states('sensor.x') | float(0) }}"}]
    assert jinja_errors(trees, cdir) == []


def test_validate_real_config_is_clean():
    # The headline regression guard: the live role config passes structural + Jinja checks.
    assert validate() == []


def test_validate_reports_structural_error(tmp_path):
    role = tmp_path / "role"
    _write(
        role / "templates/configuration.yaml.j2",
        "recorder:\n  x: 1\nrecorder:\n  y: 2\n",
    )
    _write(role / "templates/customize.yaml.j2", "{}\n")
    _write(role / "templates/ui-lovelace.yaml.j2", "{}\n")
    for s in (
        "automations.yaml",
        "scenes.yaml",
        "scripts.yaml",
        "templates.yaml",
        "rest.yaml",
    ):
        _write(role / "files" / s, "[]\n")
    (role / "files/custom_templates").mkdir(parents=True)
    errors = validate(role)
    assert any("duplicate key" in e for e in errors)


def test_uncoerced_macro_bool_uses_truth_table():
    from validate_ha_config import uncoerced_macro_bool_uses as u

    names = {"m", "n"}
    assert u("{{ m() and x }}", names) == ["m"]
    assert u("{{ x or m() }}", names) == ["m"]
    assert u("{{ not m() }}", names) == ["m"]
    assert u("{{ (m() | bool) and x }}", names) == []
    assert (
        u("{{ m() | bool and x }}", names) == []
    )  # filter binds tighter than `and` -> Filter operand
    assert u("{{ m() == 'wake' }}", names) == []
    assert u("{{ m() }}", names) == []
    assert (
        u("{{ states('x') and y }}", names) == []
    )  # unknown name, not a tracked macro
    assert u("{{ m() and n() }}", names) == ["m", "n"]  # both operands, sorted


def test_macro_bool_coercion_clean_on_real_role():
    # The real role must pass — no current macro is a raw boolean operand (error_in_scope is
    # `| bool`-coerced). Pure future-tightening; this guards against a false-positive regression.
    import validate_ha_config

    errors = validate_ha_config.validate()
    assert all("boolean and/or/not operand" not in e for e in errors), errors
