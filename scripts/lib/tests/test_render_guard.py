"""The inventory readers every generator and validator shares.

``load_yaml`` replaced three per-script copies on 2026-09-01. Each of those coerced a
non-mapping file to ``{}``; the shared one must keep doing so, or a host_vars file whose
top level is a list reaches a ``.get`` several calls later.
"""

from __future__ import annotations

from pathlib import Path

from render_guard import HOST_VARS, REPO, host_files, load_yaml
from repo_paths import ANSIBLE, INVENTORY, ROLES


def test_load_yaml_returns_a_mapping(tmp_path):
    path = tmp_path / "a.yml"
    path.write_text("key: value\n")
    assert load_yaml(path) == {"key": "value"}


def test_load_yaml_returns_empty_for_a_missing_file(tmp_path):
    assert load_yaml(tmp_path / "absent.yml") == {}


def test_load_yaml_returns_empty_for_an_empty_file(tmp_path):
    path = tmp_path / "empty.yml"
    path.write_text("")
    assert load_yaml(path) == {}


def test_load_yaml_returns_empty_for_a_non_mapping(tmp_path):
    path = tmp_path / "list.yml"
    path.write_text("- one\n- two\n")
    assert load_yaml(path) == {}


def test_host_files_skips_the_example_template(tmp_path):
    (tmp_path / "_example.yml").write_text("server_ip: 1.1.1.1\n")
    (tmp_path / "daniel-box.yml").write_text("server_ip: 2.2.2.2\n")
    assert [p.name for p in host_files(tmp_path)] == ["daniel-box.yml"]


def test_anchors_resolve_to_the_real_tree():
    """A wrong ``parents[N]`` reads as an empty inventory, not an error."""
    assert (REPO / "pyproject.toml").is_file()
    assert (ANSIBLE / "deploy.yml").is_file()
    assert (INVENTORY / "hosts.ini").is_file()
    assert (HOST_VARS / "daniel-box.yml").is_file()
    assert (ROLES / "k8s").is_dir()


def test_render_guard_re_exports_the_same_anchor_objects():
    from repo_paths import REPO as PATHS_REPO

    assert REPO == PATHS_REPO
    assert isinstance(REPO, Path)
