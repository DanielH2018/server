"""Reading the inventory: who owns each service this host declares.

`containers_list` is the source of truth. `service_owners` is the deployer's reader of D2's
`owner` flag; `ansible/tests/deploy/test_owner_readers_agree.py` holds it equal to the other
two readers.
"""

# ansible/roles/setup/gitops_deploy/tests/test_deploy_inventory.py

import pytest

from deployer_lib.deploy_inventory import service_owners


def test_service_owners_skips_a_list_before_containers_list():
    """The parse starts at the `containers_list:` key, not at the top of the file (#4964)."""
    text = (
        "earlier_list:\n"
        "  - name: earlier-svc\n"
        "    platform: k8s\n"
        "    owner: flux\n"
        "containers_list:\n"
        "  - name: wg-easy\n"
        "    platform: k8s\n"
        "    owner: flux\n"
    )
    assert service_owners(text) == {"wg-easy": "flux"}


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
