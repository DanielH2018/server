"""Tests for scripts/validate_ha_config.py — the lightweight HA config validator."""
import yaml
import pytest

from validate_ha_config import (
    HAConfigError,
    HAConfigLoader,
    ROLE_DIR,
    assemble_config,
    load_config,
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
    _write(role / "templates/configuration.yaml.j2", "homeassistant:\n  name: {{ ha_name }}\n")
    _write(role / "templates/customize.yaml.j2", "{}\n")
    _write(role / "templates/ui-lovelace.yaml.j2", "{}\n")
    with pytest.raises(HAConfigError, match="Ansible templating"):
        assemble_config(role, tmp_path / "dest")


def test_real_config_structural_clean(tmp_path):
    assemble_config(ROLE_DIR, tmp_path)
    errors, trees = load_config(tmp_path)
    assert errors == [], errors
    assert len(trees) == 2  # configuration.yaml + ui-lovelace.yaml both loaded
