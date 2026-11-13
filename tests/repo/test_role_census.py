"""`role_dirs` returns real roles and skips a retired role's `__pycache__` shell.

The census is what every guard reads `k8s/roles/` through, so its two edges, debris is skipped
and an empty directory is not, need their own coverage rather than each caller's.
"""

from _role_census import role_dirs


def test_a_retired_roles_debris_shell_is_not_a_role(tmp_path):
    """The deployer's fast-forward removes a retired role's tracked files and the gitignored
    `__pycache__/` keeps its directory, so a bare walk reads a role that no longer exists
    in git."""
    (tmp_path / "widget" / "templates").mkdir(parents=True)
    (tmp_path / "widget" / "templates" / "deployment.yaml.j2").write_text("---\n")
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


def test_the_default_census_finds_the_real_k8s_roles():
    """Non-vacuity against the real tree, and the default argument every guard relies on."""
    names = {p.name for p in role_dirs()}
    assert {"arr-notification", "image-builder", "volume-snapshot"} <= names
