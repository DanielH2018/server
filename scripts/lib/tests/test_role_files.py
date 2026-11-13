"""`role_files`: a template's lookup path, named from its role, read through the renderer's lookup.

Run: uv run pytest scripts/lib/tests/test_role_files.py
"""

import jinja2
import pytest

from renderer.renderer_lib.environment import renderer_env


@pytest.fixture
def tree(tmp_path):
    """A repo with two k8s roles, `reader` and `owner`, and a file outside every role."""
    roles = tmp_path / "ansible" / "roles" / "k8s"
    for role in ("reader", "owner"):
        (roles / role / "files").mkdir(parents=True)
        (roles / role / "templates").mkdir()
        (roles / role / "files" / "a.txt").write_text(f"{role}'s a.txt\n")
    (tmp_path / "hosts" / "lib").mkdir(parents=True)
    (tmp_path / "hosts" / "lib" / "host_lib.py").write_text("HOST = 1\n")
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


def test_a_render_with_no_role_path_is_refused(tree):
    with pytest.raises(jinja2.UndefinedError, match="role_path"):
        _render(tree, "{{ lookup('file', role_file('files/a.txt')) }}")
