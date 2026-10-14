"""Reading the inventory: which services this host declares on the k8s platform, and who owns each.

`containers_list` is the source of truth for both planes. An entry with no `platform:` key is
Docker, and `declared_k8s_services` must not return it. `service_owners` is the deployer's
reader of D2's `owner` flag; `ansible/tests/deploy/test_owner_readers_agree.py` holds it equal
to the other two readers.
"""

# ansible/roles/setup/gitops_deploy/tests/test_deploy_inventory.py

import pytest

from deployer_lib.deploy_inventory import declared_k8s_services, service_owners


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


def test_service_owners_reads_quoted_and_commented_values_and_flow_tags():
    text = (
        "containers_list:\n"
        "  - name: bento-pdf\n"
        "    platform: k8s\n"
        '    owner: "flux"  # D2.7\n'
        "  - name: n8n\n"
        "    platform: k8s\n"
        "    owner: released\n"
        "    tags: [n8n, 'n8n-images']\n"
        "  - name: sonarr\n"
        "    platform: k8s\n"
    )
    assert service_owners(text) == {
        "bento-pdf": "flux",
        "n8n": "released",
        "n8n-images": "released",
    }


def test_service_owners_skips_a_docker_entry_and_keys_after_the_list():
    """`tag_owners` reads k8s entries alone, and the last entry's block ends at the list."""
    text = (
        "containers_list:\n"
        "  - name: wg-easy\n"
        "    owner: flux\n"
        "  - name: sonarr\n"
        "    platform: k8s\n"
        "has_gitops: true\n"
        "other_list:\n"
        "  - name: sonarr-shadow\n"
        "    platform: k8s\n"
        "    owner: flux\n"
    )
    assert service_owners(text) == {}


def test_service_owners_raises_on_tags_it_cannot_parse():
    text = (
        "containers_list:\n"
        "  - name: n8n\n"
        "    platform: k8s\n"
        "    owner: flux\n"
        "    tags:\n"
        "      - n8n\n"
    )
    with pytest.raises(ValueError, match="flow list"):
        service_owners(text)
