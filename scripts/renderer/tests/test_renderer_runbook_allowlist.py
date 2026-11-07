"""The only task files left under `ansible/roles/k8s/` are the revert runbook's.

The renderer reads no role's `tasks/`, and no Ansible path applies a k8s role, so a task file
outside this set runs nowhere and reads as if it did. H7 deletes the set
(docs/target-architecture/render/r4-delete-k8s-ansible-path.md, exit 1).

Run: uv run pytest scripts/renderer/tests/test_renderer_runbook_allowlist.py
"""

from pathlib import Path

from lib.repo_paths import K8S_ROLES

RUNBOOK_ALLOWLIST = frozenset(
    {
        "volume-revert/tasks/main.yml",
        "volume-revert/tasks/claim.yml",
        "longhorn-api/tasks/main.yml",
        "longhorn-api/tasks/resolve.yml",
    }
)


def task_files(roles: Path) -> set[str]:
    """Every file under a role's `tasks/`, as `role/tasks/...`, whatever its suffix."""
    return {
        path.relative_to(roles).as_posix()
        for path in roles.glob("*/tasks/**/*")
        if path.is_file()
    }


def _seed(roles: Path, rels: set[str] | frozenset[str]) -> None:
    for rel in rels:
        path = roles / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("---\n")


def test_the_task_files_on_the_tree_are_exactly_the_allowlist():
    assert task_files(K8S_ROLES) == RUNBOOK_ALLOWLIST


def test_an_extra_task_file_is_flagged(tmp_path):
    _seed(tmp_path, RUNBOOK_ALLOWLIST | {"sonarr/tasks/sub/verify.yaml"})
    assert task_files(tmp_path) - RUNBOOK_ALLOWLIST == {"sonarr/tasks/sub/verify.yaml"}


def test_a_missing_runbook_file_is_flagged(tmp_path):
    _seed(tmp_path, RUNBOOK_ALLOWLIST - {"volume-revert/tasks/claim.yml"})
    assert RUNBOOK_ALLOWLIST - task_files(tmp_path) == {"volume-revert/tasks/claim.yml"}
