"""`role_files`: a template's lookup path, named from its role, read through the renderer's lookup.

Run: uv run pytest scripts/lib/tests/test_role_files.py
"""

import jinja2
import pytest

from lib.repo_paths import CLUSTER_VARS_SOURCE, K8S_ROLES, REPO
from lib.role_files import repo_file_path
from renderer.renderer_lib.environment import renderer_env


@pytest.fixture
def tree(tmp_path):
    """A repo with two k8s roles, `reader` and `owner`, and a file outside every role."""
    roles = tmp_path / "k8s" / "roles"
    for role in ("reader", "owner"):
        (roles / role / "files").mkdir(parents=True)
        (roles / role / "templates").mkdir()
        (roles / role / "files" / "a.txt").write_text(f"{role}'s a.txt\n")
    (tmp_path / "hosts" / "lib").mkdir(parents=True)
    (tmp_path / "hosts" / "lib" / "host_lib.py").write_text("HOST = 1\n")
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "cluster-vars.source").write_text("key sha256:abc\n")
    return roles / "reader"


def _render(role, source: str, **ctx) -> str:
    env = renderer_env(role / "templates", ctx)
    return env.from_string(source).render(ctx)


def test_an_own_role_file_reads_from_the_rendering_role(tree):
    text = _render(
        tree, "{{ lookup('file', role_file('files/a.txt')) }}", role_path=str(tree)
    )
    assert text == "reader's a.txt"


def test_a_cross_role_file_reads_from_the_named_sibling(tree):
    source = "{{ lookup('file', role_file('files/a.txt', role='owner')) }}"
    assert _render(tree, source, role_path=str(tree)) == "owner's a.txt"


def test_a_repo_file_reads_from_the_repository_root(tree):
    source = "{{ lookup('file', repo_file('hosts/lib/host_lib.py')) }}"
    assert _render(tree, source, role_path=str(tree)) == "HOST = 1"


def test_cluster_vars_source_reads_the_repositorys_digest_file(tree):
    source = "{{ lookup('file', cluster_vars_source()) }}"
    assert _render(tree, source, role_path=str(tree)) == "key sha256:abc"


def test_the_live_roles_resolve_the_live_repository_root():
    assert repo_file_path(K8S_ROLES / "crowdsec", "secrets") == REPO / "secrets"
    assert CLUSTER_VARS_SOURCE.is_file()


def test_a_render_with_no_role_path_is_refused(tree):
    with pytest.raises(jinja2.UndefinedError, match="role_path"):
        _render(tree, "{{ lookup('file', role_file('files/a.txt')) }}")
