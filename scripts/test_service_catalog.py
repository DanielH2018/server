#!/usr/bin/env python3
"""Tests for scripts/service_catalog.py.

Entirely fixture-driven — synthetic host_vars/roles under tmp_path, never the real
inventory (which changes; see scripts/test_deploy_tags.py's REAL-inventory tests for
the pattern this deliberately does NOT follow, since the catalog's job is deriving
facts, not guarding a fixed tag set).

Run: uv run pytest scripts/test_service_catalog.py
"""

from __future__ import annotations

import textwrap

import service_catalog


def _write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content))


def _make_repo(tmp_path):
    """A minimal synthetic repo: one k8s host, one docker host, group_vars, and k3s
    defaults carrying the two Longhorn tier lists. Individual tests layer roles on top."""
    host_vars = tmp_path / "host_vars"
    _write(
        host_vars / "box.yml",
        """\
        expose_mode: traefik
        containers_list:
          - name: jellyfin
            platform: k8s
            hostname: jellyfin
            use_authelia: false
          - name: authelia
            platform: k8s
            use_authelia: true
        """,
    )
    _write(
        host_vars / "pi.yml",
        """\
        expose_mode: lan
        has_gitops: false
        containers_list:
          - name: dozzle
            port: 8080
            use_authelia: false
        """,
    )
    all_vars = tmp_path / "all.yml"
    _write(all_vars, "k8s_namespace: homelab\n")
    k3s_defaults = tmp_path / "k3s_defaults.yml"
    _write(
        k3s_defaults,
        """\
        k3s_longhorn_r2_volumes:
          - homelab/authelia-config
        k3s_longhorn_weekly_volumes:
          - homelab/jellyfin-config
        """,
    )
    k8s_roles = tmp_path / "roles_k8s"
    return {
        "host_vars": host_vars,
        "all_vars": all_vars,
        "k3s_defaults": k3s_defaults,
        "k8s_roles": k8s_roles,
    }


def test_platform_defaults_to_docker_when_key_absent(tmp_path):
    paths = _make_repo(tmp_path)
    rows = service_catalog.build_rows(**paths)
    by_name = {r.name: r for r in rows}
    assert by_name["jellyfin"].platform == "k8s"
    assert by_name["dozzle"].platform == "docker"


def test_auth_tier_true_false_and_missing(tmp_path):
    paths = _make_repo(tmp_path)
    # Add an entry with no use_authelia key at all.
    _write(
        paths["host_vars"] / "box.yml",
        """\
        expose_mode: traefik
        containers_list:
          - name: jellyfin
            platform: k8s
            hostname: jellyfin
            use_authelia: false
          - name: authelia
            platform: k8s
            use_authelia: true
          - name: mystery
            platform: k8s
        """,
    )
    rows = service_catalog.build_rows(**paths)
    by_name = {r.name: r for r in rows}
    assert by_name["authelia"].auth_tier == "Authelia"
    assert by_name["jellyfin"].auth_tier == "none (public/no-auth)"
    assert by_name["mystery"].auth_tier.startswith("unknown")


def test_k8s_route_uses_hostname_default_name_when_ingressroute_exists(tmp_path):
    paths = _make_repo(tmp_path)
    role = paths["k8s_roles"] / "jellyfin" / "templates"
    _write(role / "ingressroute.yaml.j2", "{{ ingressroute(...) }}\n")
    rows = service_catalog.build_rows(**paths)
    row = next(r for r in rows if r.name == "jellyfin")
    assert row.route.startswith("jellyfin.local.<domain>")


def test_k8s_route_is_no_route_when_role_has_no_ingressroute_template(tmp_path):
    paths = _make_repo(tmp_path)
    # authelia gets no ingressroute.yaml.j2 in this fixture (e.g. an infra-only role).
    rows = service_catalog.build_rows(**paths)
    row = next(r for r in rows if r.name == "authelia")
    assert row.route == "no route (infra role)"


def test_docker_route_is_lan_direct_when_expose_mode_lan(tmp_path):
    paths = _make_repo(tmp_path)
    rows = service_catalog.build_rows(**paths)
    row = next(r for r in rows if r.name == "dozzle")
    assert row.route == "LAN-direct (no Traefik route)"


def test_docker_route_is_unknown_when_expose_mode_not_lan(tmp_path):
    paths = _make_repo(tmp_path)
    _write(
        paths["host_vars"] / "pi.yml",
        """\
        expose_mode: traefik
        has_gitops: false
        containers_list:
          - name: dozzle
            port: 8080
            use_authelia: false
        """,
    )
    rows = service_catalog.build_rows(**paths)
    row = next(r for r in rows if r.name == "dozzle")
    assert row.route.startswith("unknown")


def test_backup_tier_literal_pvc_name_in_r2_list(tmp_path):
    paths = _make_repo(tmp_path)
    role = paths["k8s_roles"] / "authelia" / "templates"
    _write(
        role / "pvc.yaml.j2",
        """\
        apiVersion: v1
        kind: PersistentVolumeClaim
        metadata:
          name: authelia-config
        """,
    )
    rows = service_catalog.build_rows(**paths)
    row = next(r for r in rows if r.name == "authelia")
    assert row.backup_tier == "daily -> R2"


def test_backup_tier_literal_pvc_name_in_weekly_list(tmp_path):
    paths = _make_repo(tmp_path)
    role = paths["k8s_roles"] / "jellyfin" / "templates"
    _write(
        role / "pvc.yaml.j2",
        """\
        apiVersion: v1
        kind: PersistentVolumeClaim
        metadata:
          name: jellyfin-config
        """,
    )
    rows = service_catalog.build_rows(**paths)
    row = next(r for r in rows if r.name == "jellyfin")
    assert row.backup_tier == "weekly -> B2 (default target)"


def test_backup_tier_pvc_not_in_either_list_defaults_to_daily_b2(tmp_path):
    paths = _make_repo(tmp_path)
    role = paths["k8s_roles"] / "jellyfin" / "templates"
    _write(
        role / "pvc.yaml.j2",
        """\
        apiVersion: v1
        kind: PersistentVolumeClaim
        metadata:
          name: some-other-claim
        """,
    )
    rows = service_catalog.build_rows(**paths)
    row = next(r for r in rows if r.name == "jellyfin")
    assert row.backup_tier == "daily -> B2 (default group)"


def test_backup_tier_no_pvc_is_stateless_not_unknown(tmp_path):
    paths = _make_repo(tmp_path)
    rows = service_catalog.build_rows(**paths)
    row = next(r for r in rows if r.name == "authelia")
    assert row.backup_tier == "no PVC (stateless)"


def test_backup_tier_unresolvable_var_is_unknown(tmp_path):
    paths = _make_repo(tmp_path)
    role = paths["k8s_roles"] / "jellyfin" / "templates"
    _write(
        role / "pvc.yaml.j2",
        """\
        apiVersion: v1
        kind: PersistentVolumeClaim
        metadata:
          name: {{ jellyfin_k8s_claim }}
        """,
    )
    # No defaults/main.yml giving jellyfin_k8s_claim a literal value.
    rows = service_catalog.build_rows(**paths)
    row = next(r for r in rows if r.name == "jellyfin")
    assert row.backup_tier.startswith("unknown")


def test_backup_tier_var_resolved_from_role_defaults(tmp_path):
    paths = _make_repo(tmp_path)
    role_templates = paths["k8s_roles"] / "jellyfin" / "templates"
    _write(
        role_templates / "pvc.yaml.j2",
        """\
        apiVersion: v1
        kind: PersistentVolumeClaim
        metadata:
          name: {{ jellyfin_k8s_claim }}
        """,
    )
    _write(
        paths["k8s_roles"] / "jellyfin" / "defaults" / "main.yml",
        "jellyfin_k8s_claim: jellyfin-config\n",
    )
    rows = service_catalog.build_rows(**paths)
    row = next(r for r in rows if r.name == "jellyfin")
    assert row.backup_tier == "weekly -> B2 (default target)"


def test_backup_tier_finds_pvc_referenced_only_by_claimname(tmp_path):
    # home-assistant's real shape: no pvc.yaml.j2 of its own, just a claimName: reference
    # to a PVC provisioned elsewhere in a deployment/pod template.
    paths = _make_repo(tmp_path)
    role_templates = paths["k8s_roles"] / "jellyfin" / "templates"
    _write(
        role_templates / "deployment.yaml.j2",
        """\
        volumes:
          - name: config
            persistentVolumeClaim:
              claimName: jellyfin-config
        """,
    )
    rows = service_catalog.build_rows(**paths)
    row = next(r for r in rows if r.name == "jellyfin")
    assert row.backup_tier == "weekly -> B2 (default target)"


def test_backup_tier_docker_is_not_longhorn(tmp_path):
    paths = _make_repo(tmp_path)
    rows = service_catalog.build_rows(**paths)
    row = next(r for r in rows if r.name == "dozzle")
    assert row.backup_tier == "n/a (Docker/Pi, not Longhorn-backed)"


def test_autodeploy_eligible_true(tmp_path):
    paths = _make_repo(tmp_path)
    _write(
        paths["k8s_roles"] / "jellyfin" / "defaults" / "main.yml",
        "k8s_autodeploy: true\nk8s_autodeploy_reason: image-pinned, low blast radius\n",
    )
    rows = service_catalog.build_rows(**paths)
    row = next(r for r in rows if r.name == "jellyfin")
    assert row.autodeploy == "eligible"


def test_autodeploy_denylisted_with_reason(tmp_path):
    paths = _make_repo(tmp_path)
    _write(
        paths["k8s_roles"] / "jellyfin" / "defaults" / "main.yml",
        "k8s_autodeploy: false\nk8s_autodeploy_reason: shared media volume coupling\n",
    )
    rows = service_catalog.build_rows(**paths)
    row = next(r for r in rows if r.name == "jellyfin")
    assert row.autodeploy == "denylisted (shared media volume coupling)"


def test_autodeploy_unknown_when_role_declares_nothing(tmp_path):
    paths = _make_repo(tmp_path)
    rows = service_catalog.build_rows(**paths)
    row = next(r for r in rows if r.name == "jellyfin")
    assert row.autodeploy.startswith("unknown")


def test_autodeploy_docker_is_na_when_host_has_no_gitops(tmp_path):
    paths = _make_repo(tmp_path)
    rows = service_catalog.build_rows(**paths)
    row = next(r for r in rows if r.name == "dozzle")
    assert row.autodeploy == "n/a (host has no GitOps auto-deploy path)"


def test_underscore_prefixed_host_vars_files_are_excluded(tmp_path):
    paths = _make_repo(tmp_path)
    _write(
        paths["host_vars"] / "_example.yml",
        """\
        containers_list:
          - name: not-a-real-service
            platform: k8s
        """,
    )
    rows = service_catalog.build_rows(**paths)
    assert "not-a-real-service" not in {r.name for r in rows}


def test_multiple_pvcs_report_each_tier_deduplicated(tmp_path):
    paths = _make_repo(tmp_path)
    role_templates = paths["k8s_roles"] / "jellyfin" / "templates"
    _write(
        role_templates / "pvc.yaml.j2",
        """\
        apiVersion: v1
        kind: PersistentVolumeClaim
        metadata:
          name: jellyfin-config
        ---
        apiVersion: v1
        kind: PersistentVolumeClaim
        metadata:
          name: authelia-config
        """,
    )
    rows = service_catalog.build_rows(**paths)
    row = next(r for r in rows if r.name == "jellyfin")
    assert row.backup_tier == "weekly -> B2 (default target); daily -> R2"


def test_render_html_is_self_contained_and_lists_every_service(tmp_path):
    paths = _make_repo(tmp_path)
    rows = service_catalog.build_rows(**paths)
    out = service_catalog.render_html(rows)
    assert "<!doctype html>" in out
    assert "<style>" in out
    assert "http" not in out.split("<style>")[0]  # no external asset before the CSS
    for row in rows:
        assert row.name in out


def test_main_writes_output_file(tmp_path):
    paths = _make_repo(tmp_path)
    out_file = tmp_path / "catalog.html"
    exit_code = service_catalog.main(
        [
            "--out",
            str(out_file),
            "--host-vars",
            str(paths["host_vars"]),
            "--k8s-roles",
            str(paths["k8s_roles"]),
            "--k3s-defaults",
            str(paths["k3s_defaults"]),
            "--all-vars",
            str(paths["all_vars"]),
        ]
    )
    assert exit_code == 0
    assert out_file.is_file()
    assert "Homelab Service Catalog" in out_file.read_text()
