"""The synthetic repo every service-catalogue test builds on.

One k8s host, one docker host, group_vars, and k3s defaults carrying the three Longhorn tier
lists; each test layers roles on top under `paths["k8s_roles"]`. Shared by
test_service_catalog.py and test_catalog_backup.py, which is why scripts/docs/tests is on
pytest's `pythonpath` (pyproject.toml) the way scripts/deploy_tools/tests is for _land_fakes.py.
"""

import textwrap


def write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content))


def make_repo(tmp_path):
    """Build a minimal synthetic repo for the catalog tests; see the module docstring."""
    host_vars = tmp_path / "host_vars"
    write(
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
    write(
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
    write(all_vars, "k8s_namespace: homelab\n")
    k3s_defaults = tmp_path / "k3s_defaults.yml"
    write(
        k3s_defaults,
        """\
        k3s_longhorn_r2_volumes:
          - homelab/authelia-config
        k3s_longhorn_weekly_volumes:
          - homelab/jellyfin-config
        k3s_longhorn_nobackup_volumes:
          - homelab/uptime-kuma-data
        """,
    )
    k8s_roles = tmp_path / "roles_k8s"
    return {
        "host_vars": host_vars,
        "all_vars": all_vars,
        "k3s_defaults": k3s_defaults,
        "k8s_roles": k8s_roles,
    }
