"""The synthetic repo every service-catalogue test builds on.

The k8s service list, group_vars, and k3s defaults carrying the three Longhorn tier
lists; each test layers roles on top under `paths["k8s_roles"]`. Shared by
test_service_catalog.py and test_catalog_backup.py, which is why scripts/docs/tests is on
pytest's `pythonpath` (pyproject.toml) the way scripts/deploy_tools/tests is for _land_fakes.py.
"""

import textwrap

from lib.estate import Inventory


def write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content))


def make_repo(tmp_path):
    """Build a minimal synthetic repo for the catalog tests; see the module docstring."""
    services = tmp_path / "services.yml"
    write(
        services,
        """\
        containers_list:
          - name: jellyfin
            hostname: jellyfin
            use_authelia: false
          - name: authelia
            use_authelia: true
        """,
    )
    estate_vars = tmp_path / "estate-vars.yml"
    write(estate_vars, "k8s_namespace: homelab\n")
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
    for role in ("jellyfin", "authelia"):
        write(k8s_roles / role / "defaults" / "main.yml", "---\n")
    return {
        "services": services,
        "estate_vars": estate_vars,
        "k3s_defaults": k3s_defaults,
        "k8s_roles": k8s_roles,
    }


def catalog_args(paths):
    """`service_catalog.build_rows`'s keyword arguments over the synthetic repo `paths`."""
    return {
        "inventory": Inventory(
            estate_vars=paths["estate_vars"],
            k8s_vars=paths["estate_vars"].parent / "k8s-vars.yml",
            host_vars=paths["estate_vars"].parent / "hosts",
            services=paths["services"],
        ),
        "k8s_roles": paths["k8s_roles"],
        "k3s_defaults": paths["k3s_defaults"],
    }
