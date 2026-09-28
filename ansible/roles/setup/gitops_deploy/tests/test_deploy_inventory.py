"""Reading the inventory: which services this host declares on the k8s platform.

`containers_list` is the source of truth for both planes. An entry with no `platform:` key is
Docker, and `declared_k8s_services` must not return it.
"""

# ansible/roles/setup/gitops_deploy/tests/test_deploy_inventory.py

from deploy_inventory import declared_k8s_services


def test_declared_k8s_services_parses_platform_k8s_entries():
    text = (
        "containers_list:\n"
        "  - name: wg-easy\n"
        "    platform: k8s\n"
        "    port: 51821\n"
        "  - name: traefik\n"
        "    port: 8080\n"
    )
    assert declared_k8s_services(text) == {"wg-easy"}


def test_declared_k8s_services_excludes_docker_entries():
    text = "containers_list:\n  - name: traefik\n    port: 8080\n"
    assert declared_k8s_services(text) == set()
