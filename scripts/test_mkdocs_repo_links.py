"""The repo-path link hook, and the thing that can break it silently.

`mkdocs build --strict` validates markdown links while rendering; `on_page_content` runs
after that, so nothing upstream checks a single href this hook emits. These tests are the
gate, and they carry the two halves that must agree: the anchors injected into the Scripts
page, and the links pointed at them.
"""

from __future__ import annotations

from pathlib import Path

import markdown
import pytest
import yaml

import _mkdocs_repo_links as hook

REPO = Path(__file__).resolve().parent.parent
DOCS = REPO / "docs"
SCRIPTS_MD = DOCS / "reference/scripts.md"

# Built pages, mirroring what `on_files` hands the hook. mkdocs excludes these two, so the
# real index never holds a key for them -- a link to an unbuilt page would 404.
_EXCLUDED = ("superpowers/", "adr/template.md")


def _markdown_extensions() -> list[str]:
    """The extensions mkdocs.yml configures, so a fixture renders the way the site does."""

    class _Loader(yaml.SafeLoader):
        pass

    _Loader.add_multi_constructor(
        "tag:yaml.org,2002:python/name:", lambda loader, suffix, node: suffix
    )
    _Loader.add_multi_constructor("!", lambda loader, suffix, node: suffix)
    config = yaml.load((REPO / "mkdocs.yml").read_text(), Loader=_Loader)
    return [
        entry if isinstance(entry, str) else next(iter(entry))
        for entry in config["markdown_extensions"]
    ]


def _render(text: str) -> str:
    return markdown.markdown(text, extensions=_markdown_extensions())


@pytest.fixture(scope="module")
def names() -> set[str]:
    return hook.script_names(SCRIPTS_MD.read_text())


@pytest.fixture(scope="module")
def index(names: set[str]) -> dict[str, tuple[str, str]]:
    doc_uris = {
        path.relative_to(DOCS).as_posix()
        for path in DOCS.rglob("*.md")
        if not path.relative_to(DOCS).as_posix().startswith(_EXCLUDED)
    }
    return hook.build_index(doc_uris, names, hook.shared_basenames(REPO, names))


def test_every_script_on_the_page_is_read_off_it(names: set[str]) -> None:
    assert "service_catalog.py" in names
    assert "probe.py" in names
    # The Tests column is code spans too; only the first cell of a row is a script.
    assert not any(name.startswith("test_") for name in names)


def test_every_documented_script_gets_an_anchor(names: set[str]) -> None:
    """The regex must match Python-Markdown's real table HTML, not a guess at it.

    A silent miss here drops all 47 anchors and leaves every link pointing at a fragment
    that does not exist -- which still renders as a working link to the top of the page.
    """
    anchored = hook.add_script_anchors(_render(SCRIPTS_MD.read_text()), names)
    missing = {
        name for name in names if f'id="{hook.anchor_for(name)}"' not in anchored
    }
    assert not missing


def test_an_anchor_is_injected_once_per_script(names: set[str]) -> None:
    anchored = hook.add_script_anchors(_render(SCRIPTS_MD.read_text()), names)
    assert anchored.count('<code id="script-') == len(names)


def test_a_script_resolves_by_both_forms(index: dict[str, tuple[str, str]]) -> None:
    target = (hook.SCRIPTS_PAGE, "script-service_catalog-py")
    assert hook.lookup(index, "scripts/service_catalog.py") == target
    assert hook.lookup(index, "service_catalog.py") == target


def test_a_line_reference_resolves_to_the_page(
    index: dict[str, tuple[str, str]],
) -> None:
    """The repo writes `file:line`; the line is not addressable, the file is."""
    assert hook.lookup(index, "scripts/probe.py:75") == hook.lookup(
        index, "scripts/probe.py"
    )
    assert hook.lookup(index, "scripts/probe.py:75-80") is not None


def test_a_docs_page_resolves_to_itself(index: dict[str, tuple[str, str]]) -> None:
    assert hook.lookup(index, "docs/secret-rotation.md") == ("secret-rotation.md", "")
    assert hook.lookup(index, "docs/reference/services.md") == (
        "reference/services.md",
        "",
    )


@pytest.mark.parametrize(
    "path",
    [
        # Config the site has no page about.
        "mkdocs.yml",
        "pyproject.toml",
        # A role file. The Services page documents the SERVICE -- route, auth, backup tier --
        # and nothing about either of these, which would land on the same row regardless.
        "ansible/roles/k8s/netpol-baseline/tasks/main.yml",
        "ansible/roles/k8s/netpol-baseline/defaults/main.yml",
        "tasks/main.yml",
        "defaults/main.yml",
        # Operator docs that live in the tree, not on the site.
        "ansible/roles/setup/gitops_deploy/CLAUDE.md",
        "CLAUDE.md",
        # A test, and a module that is not a first-party script.
        "test_service_catalog.py",
        "deploy_logic.py",
    ],
)
def test_a_path_the_site_does_not_document_stays_plain(
    index: dict[str, tuple[str, str]], path: str
) -> None:
    assert hook.lookup(index, path) is None


def test_the_bare_form_is_suppressed_when_the_tree_holds_two() -> None:
    """`check.py` and `main.yml` name files outside scripts/; a bare mention is ambiguous."""
    shared = hook.shared_basenames(REPO, {"check.py", "main.yml", "service_catalog.py"})
    assert shared == {"check.py", "main.yml"}


def test_the_basename_scan_ignores_the_virtualenv() -> None:
    """`.venv` vendors a urllib3 `probe.py`; letting it count would unlink every bare one."""
    assert hook.shared_basenames(REPO, {"probe.py"}) == set()


def test_every_target_is_a_page_that_exists(index: dict[str, tuple[str, str]]) -> None:
    for src_uri, _ in index.values():
        assert (DOCS / src_uri).is_file(), src_uri


def test_every_anchor_is_one_the_hook_injects(
    index: dict[str, tuple[str, str]], names: set[str]
) -> None:
    """The two halves cannot drift: both are derived from the same set of script names."""
    anchors = {anchor for _, anchor in index.values() if anchor}
    assert anchors == {hook.anchor_for(name) for name in names}


def _resolve(text: str) -> str | None:
    return (
        "../scripts/#script-sample_tool-py"
        if text == "scripts/sample_tool.py"
        else None
    )


def test_a_resolvable_span_is_wrapped_in_a_link() -> None:
    linked = hook.link_paths(
        "<p>run <code>scripts/sample_tool.py</code> now</p>", _resolve
    )
    assert '<a class="repo-path" href="../scripts/#script-sample_tool-py">' in linked
    assert "<code>scripts/sample_tool.py</code></a>" in linked


def test_an_unresolvable_span_is_untouched() -> None:
    original = "<p>see <code>ansible/deploy.yml</code></p>"
    assert hook.link_paths(original, _resolve) == original


@pytest.mark.parametrize(
    "html",
    [
        # A fenced block shows a command to run, not a file to go and read.
        "<pre><code>uv run scripts/sample_tool.py --tags docs</code></pre>",
        # Already a link: nesting an anchor inside one produces invalid HTML.
        '<p><a href="../deploying/"><code>scripts/sample_tool.py</code></a></p>',
        # A heading carries its own permalink, and the Usage headings on the Scripts page
        # would otherwise link two screens up the page they are already on.
        '<h3 id="sample-tool"><code>scripts/sample_tool.py</code></h3>',
        # The id says this cell IS the target; linking it would point it at itself.
        '<td><code id="script-sample_tool-py">scripts/sample_tool.py</code></td>',
    ],
)
def test_a_protected_region_is_left_alone(html: str) -> None:
    assert hook.link_paths(html, _resolve) == html


def test_text_around_a_protected_region_is_still_linked() -> None:
    linked = hook.link_paths(
        "<p><code>scripts/sample_tool.py</code></p>"
        "<pre><code>scripts/sample_tool.py</code></pre>"
        "<p><code>scripts/sample_tool.py</code></p>",
        _resolve,
    )
    assert linked.count('class="repo-path"') == 2
