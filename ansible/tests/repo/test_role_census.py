"""`role_dirs` returns real roles and skips a retired role's `__pycache__` shell.

The census is what every guard reads a role tree through — `roles/k8s/`, `roles/setup/` and
the Pi's `roles/containers/` alike — so its two edges, debris is skipped and an empty
directory is not, need their own coverage rather than each caller's.
"""

from _helpers import CONTAINER_ROLES, SETUP_ROLES
from _role_census import role_dirs, task_files, task_files_by_role


def test_a_retired_roles_debris_shell_is_not_a_role(tmp_path):
    """The deployer's fast-forward removes a retired role's tracked files and the gitignored
    `__pycache__/` keeps its directory, so a bare walk reads a role that no longer exists
    in git."""
    (tmp_path / "widget" / "tasks").mkdir(parents=True)
    (tmp_path / "widget" / "tasks" / "main.yml").write_text("[]\n")
    (tmp_path / "retired" / "__pycache__").mkdir(parents=True)
    (tmp_path / "retired" / "__pycache__" / "filter.pyc").write_bytes(b"\x00")

    assert [p.name for p in role_dirs(tmp_path)] == ["widget"]


def test_an_empty_directory_is_still_a_role(tmp_path):
    """git cannot leave an empty directory behind, so one exists because a person or a script
    made it — and a caller passing its own `roles_dir` (the Renovate guards build synthetic
    roles) must still see what it built."""
    (tmp_path / "hand-made").mkdir()

    assert [p.name for p in role_dirs(tmp_path)] == ["hand-made"]


def test_a_file_beside_the_roles_is_not_a_role(tmp_path):
    (tmp_path / "README.md").write_text("not a role\n")

    assert role_dirs(tmp_path) == []


def test_a_nested_task_file_is_credited_to_its_role(tmp_path):
    """`parent.parent` names `tasks` for `tasks/sub/b.yml`; the pairs must name the role."""
    (tmp_path / "widget" / "tasks" / "sub").mkdir(parents=True)
    (tmp_path / "widget" / "tasks" / "a.yml").write_text("[]\n")
    (tmp_path / "widget" / "tasks" / "sub" / "b.yml").write_text("[]\n")

    pairs = task_files_by_role(tmp_path)

    assert [(role.name, path.name) for role, path in pairs] == [
        ("widget", "a.yml"),
        ("widget", "b.yml"),
    ]
    assert [path for _role, path in pairs] == task_files(tmp_path)


def test_the_default_census_finds_the_real_k8s_roles():
    """Non-vacuity against the real tree, and the default argument every guard relies on."""
    names = {p.name for p in role_dirs()}
    assert {"manifests", "image-builder", "cronjob-gate"} <= names


def test_the_census_finds_the_real_setup_and_pi_roles():
    """Non-vacuity for the setup and Pi trees, each named by a role."""
    assert {"common", "gitops_deploy"} <= {p.name for p in role_dirs(SETUP_ROLES)}
    assert {"wg-easy", "docker-proxy"} <= {p.name for p in role_dirs(CONTAINER_ROLES)}
