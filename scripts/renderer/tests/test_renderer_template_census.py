"""Every filter and test name the k8s templates use resolves in the renderer's environment.

The census parses each template under the k8s roles' `templates/` and `ansible/templates/`
through `renderer_lib.cluster.renderer_env`, the environment `render_tag` renders with. A name
the environment lacks would otherwise surface only when a render first reaches that template,
and a template behind a role that does not render yet would hide it.

Run: uv run pytest scripts/renderer/tests/test_renderer_template_census.py
"""

from pathlib import Path

from jinja2 import nodes

from lib.render_guard import SHARED_TPL
from lib.repo_paths import K8S_ROLES
from renderer.renderer_lib.cluster import LABEL_WRAPPER, renderer_env


def _corpus() -> list[Path]:
    return sorted(K8S_ROLES.glob("*/templates/**/*.j2")) + sorted(
        SHARED_TPL.glob("*.j2")
    )


def unresolved(paths: list[Path]) -> dict[str, list[str]]:
    """Each `filter <name>` or `test <name>` the renderer's environment lacks, with its templates."""
    env = renderer_env(SHARED_TPL)
    missing: dict[str, list[str]] = {}
    for path in paths:
        for node in env.parse(path.read_text()).find_all((nodes.Filter, nodes.Test)):
            kind, table = (
                ("filter", env.filters)
                if isinstance(node, nodes.Filter)
                else ("test", env.tests)
            )
            if node.name not in table:
                missing.setdefault(f"{kind} {node.name}", []).append(str(path))
    return missing


def _users(paths: list[Path], filter_name: str) -> set[Path]:
    env = renderer_env(SHARED_TPL)
    return {
        path
        for path in paths
        if any(
            node.name == filter_name
            for node in env.parse(path.read_text()).find_all(nodes.Filter)
        )
    }


def test_every_filter_and_test_in_the_corpus_resolves():
    assert unresolved(_corpus()) == {}


def test_the_census_reads_the_labelling_wrapper():
    """Non-vacuity: the one template that needs `homelab_role_label` is in the corpus."""
    corpus = _corpus()
    assert len(corpus) >= 290
    assert LABEL_WRAPPER in _users(corpus, "homelab_role_label")


def test_a_template_with_an_unregistered_filter_is_flagged(tmp_path):
    bad = tmp_path / "bad.yaml.j2"
    bad.write_text(
        "value: {{ name | no_such_filter }}\n{% if x is no_such_test %}{% endif %}\n"
    )
    assert unresolved([bad]) == {
        "filter no_such_filter": [str(bad)],
        "test no_such_test": [str(bad)],
    }


def test_a_template_with_registered_names_is_clean(tmp_path):
    good = tmp_path / "good.yaml.j2"
    good.write_text(
        "value: {{ name | from_json | to_json }}\n{% if x is search('y') %}{% endif %}\n"
    )
    assert unresolved([good]) == {}
