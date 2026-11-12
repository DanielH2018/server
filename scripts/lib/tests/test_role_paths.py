"""The path-to-role mappers `land.sh` derives its tags through.

Run: uv run pytest scripts/lib/tests/test_role_paths.py
"""

import pytest

from lib.role_paths import RolePath, is_test_path, role_for, role_of, tag_for


def test_role_of_names_the_plane_role_and_subdirectory():
    assert role_of("ansible/roles/k8s/sonarr/templates/deployment.yaml.j2") == RolePath(
        "k8s", "sonarr", "templates"
    )
    assert role_of("ansible/roles/k8s/longhorn/CLAUDE.md") == RolePath(
        "k8s", "longhorn", ""
    )


def test_role_of_names_nothing_outside_a_role():
    assert role_of("ansible/templates/macros.j2") is None


@pytest.mark.parametrize(
    "path",
    [
        "ansible/roles/k8s/volume-revert/CLAUDE.md",
        "hosts/components/k3s-server/defaults/main.yml",
    ],
    ids=["a-document", "a-setup-role"],
)
def test_role_for_names_no_role_for_a_document_or_a_setup_path(path):
    assert role_for(path) is None


@pytest.mark.parametrize(
    "path",
    [
        "ansible/tests/deploy/test_k3s_pin_routing.py",
        "ansible/roles/k8s/monitor-bridge/tests/test_b2_storage.py",
        "scripts/conftest.py",
        "ansible/roles/k8s/sonarr/files/test_helper.py",
    ],
)
def test_a_test_path_is_flagged(path):
    assert is_test_path(path)


def test_a_role_shipped_file_is_no_test_path():
    assert not is_test_path("ansible/roles/k8s/monitor-bridge/files/checks/service.py")


def test_tag_for_keeps_only_a_declared_role():
    path = "ansible/roles/k8s/volume-revert/tasks/main.yml"
    assert role_for(path) == "volume-revert"
    assert tag_for(path, {"sonarr"}) is None
    assert tag_for(path, {"volume-revert"}) == "volume-revert"


def test_tag_for_drops_a_declared_roles_tests():
    assert tag_for("ansible/roles/k8s/sonarr/tests/test_x.py", {"sonarr"}) is None
