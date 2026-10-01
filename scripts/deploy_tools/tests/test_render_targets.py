#!/usr/bin/env python3
"""Tests for the render-record tag list.

Run: uv run pytest scripts/deploy_tools/tests/test_render_targets.py
"""

import textwrap

import render_targets

_INCLUDER = """\
- name: Render and apply the manifests
  ansible.builtin.include_role:
    name: k8s/manifests
"""


def _role(roles, name, tasks):
    (roles / name / "tasks").mkdir(parents=True)
    (roles / name / "tasks" / "main.yml").write_text(tasks)


def test_only_local_k8s_manifests_includers(tmp_path):
    host_vars, roles = tmp_path / "host_vars", tmp_path / "roles"
    host_vars.mkdir()
    (host_vars / "box.yml").write_text(
        textwrap.dedent(
            """\
            containers_list:
              - name: sonarr
                platform: k8s
              - name: raw-apply
                platform: k8s
              - name: dozzle
            """
        )
    )
    (host_vars / "pi.yml").write_text(
        "containers_list:\n  - name: remote\n    platform: k8s\n"
    )
    for name in ("sonarr", "remote"):
        _role(roles, name, _INCLUDER)
    # A task NAME mentioning the role is not an include of it.
    _role(roles, "raw-apply", "- name: Apply like k8s/manifests does\n  command: x\n")

    assert render_targets.render_targets("box", host_vars, roles) == ["sonarr"]


def test_real_inventory_renders_stamped_services_and_skips_a_shared_role():
    # Named members, so a derivation that silently empties is caught: jellyfin is a service
    # whose dry-run must render, n8n builds its own images and renders too, and volume-claim
    # is a shared role no containers_list names.
    tags = render_targets.render_targets("daniel-box")
    assert "jellyfin" in tags
    assert "n8n" in tags
    assert "volume-claim" not in tags


def test_cli_exits_one_for_a_host_with_nothing_to_render(capsys):
    assert render_targets.main(["no-such-host"]) == 1
    assert "no renderable k8s service" in capsys.readouterr().err
