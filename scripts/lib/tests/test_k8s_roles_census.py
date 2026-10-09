"""`role_dirs` is the one census of a role tree under `scripts/`.

Each reader passes the names it skips on purpose as `exclude`; a retired role's
`__pycache__`-only shell is dropped for every reader alike.
"""

from lib.k8s_roles import K8S_ROLES, SKIP_ROLES, role_dirs


def _tree(tmp_path):
    (tmp_path / "real" / "defaults").mkdir(parents=True)
    (tmp_path / "real" / "defaults" / "main.yml").write_text("x: 1\n")
    (tmp_path / "helper" / "tasks").mkdir(parents=True)
    (tmp_path / "helper" / "tasks" / "main.yml").write_text("[]\n")
    (tmp_path / "ghost" / "files" / "__pycache__").mkdir(parents=True)
    (tmp_path / "ghost" / "files" / "__pycache__" / "x.pyc").write_bytes(b"")
    return tmp_path


def test_a_pycache_only_shell_is_not_a_role(tmp_path):
    assert [d.name for d in role_dirs(_tree(tmp_path))] == ["helper", "real"]


def test_an_excluded_name_is_dropped(tmp_path):
    assert [d.name for d in role_dirs(_tree(tmp_path), exclude={"helper"})] == ["real"]


def test_the_live_tree_census_names_known_roles():
    names = {d.name for d in role_dirs(K8S_ROLES)}
    assert {"traefik", "authelia", "manifests"} <= names
    assert not SKIP_ROLES & {d.name for d in role_dirs(K8S_ROLES, exclude=SKIP_ROLES)}
