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

REPO = Path(__file__).resolve().parent.parent.parent
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
def paths() -> set[str]:
    return hook.script_paths(SCRIPTS_MD.read_text())


@pytest.fixture(scope="module")
def index(paths: set[str]) -> dict[str, tuple[str, str]]:
    doc_uris = {
        path.relative_to(DOCS).as_posix()
        for path in DOCS.rglob("*.md")
        if not path.relative_to(DOCS).as_posix().startswith(_EXCLUDED)
    }
    return hook.build_index(doc_uris, paths, hook.ambiguous_basenames(REPO, paths))


def test_every_script_on_the_page_is_read_off_it(paths: set[str]) -> None:
    assert "scripts/docs/service_catalog.py" in paths
    assert "scripts/diagnostics/probe.py" in paths
    # The Tests column is code spans too; only the first cell of a row is a script.
    assert all(path.startswith("scripts/") for path in paths)


def test_every_documented_script_gets_an_anchor(paths: set[str]) -> None:
    """The regex must match Python-Markdown's real table HTML, not a guess at it.

    A silent miss here drops every anchor and leaves each link pointing at a fragment that
    does not exist -- which still renders as a working link to the top of the page.
    """
    anchored = hook.add_script_anchors(_render(SCRIPTS_MD.read_text()), paths)
    missing = {
        path for path in paths if f'id="{hook.anchor_for(path)}"' not in anchored
    }
    assert not missing


def test_an_anchor_is_injected_once_per_script(paths: set[str]) -> None:
    anchored = hook.add_script_anchors(_render(SCRIPTS_MD.read_text()), paths)
    assert anchored.count('<code id="script-') == len(paths)


def test_the_anchor_keeps_the_subdirectory(paths: set[str]) -> None:
    """Two scripts can share a basename, so the directory has to be in the id."""
    assert hook.anchor_for("scripts/docs/build_docs.py") == "script-docs-build_docs-py"
    assert len({hook.anchor_for(path) for path in paths}) == len(paths)


def test_a_script_resolves_by_both_forms(index: dict[str, tuple[str, str]]) -> None:
    target = (hook.SCRIPTS_PAGE, "script-docs-service_catalog-py")
    assert hook.lookup(index, "scripts/docs/service_catalog.py") == target
    assert hook.lookup(index, "service_catalog.py") == target


def test_a_line_reference_resolves_to_the_page(
    index: dict[str, tuple[str, str]],
) -> None:
    """The repo writes `file:line`; the line is not addressable, the file is."""
    full = "scripts/diagnostics/probe.py"
    assert hook.lookup(index, f"{full}:75") == hook.lookup(index, full)
    assert hook.lookup(index, f"{full}:75-80") is not None


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
        # The pre-reorganisation path. Linking it would hide the staleness.
        "scripts/service_catalog.py",
    ],
)
def test_a_path_the_site_does_not_document_stays_plain(
    index: dict[str, tuple[str, str]], path: str
) -> None:
    assert hook.lookup(index, path) is None


def test_a_basename_a_tree_file_shares_is_ambiguous() -> None:
    """A script named `check.py` could not claim the bare form: monitor-bridge has one."""
    assert hook.ambiguous_basenames(REPO, {"scripts/dev/check.py"}) == {"check.py"}


def test_two_scripts_sharing_a_basename_are_both_ambiguous() -> None:
    """The collision need not involve a file outside the Scripts page at all."""
    twins = {"scripts/dev/report.py", "scripts/backup/report.py"}
    assert hook.ambiguous_basenames(REPO, twins) == {"report.py"}


def test_the_basename_scan_ignores_the_virtualenv() -> None:
    """`.venv` vendors a urllib3 `probe.py`; letting it count would unlink every bare one."""
    assert hook.ambiguous_basenames(REPO, {"scripts/diagnostics/probe.py"}) == set()


def test_every_target_is_a_page_that_exists(index: dict[str, tuple[str, str]]) -> None:
    for src_uri, _ in index.values():
        assert (DOCS / src_uri).is_file(), src_uri


def test_every_anchor_is_one_the_hook_injects(
    index: dict[str, tuple[str, str]], paths: set[str]
) -> None:
    """The two halves cannot drift: both are derived from the same set of script paths."""
    anchors = {anchor for _, anchor in index.values() if anchor}
    assert anchors == {hook.anchor_for(path) for path in paths}


_SAMPLE = "scripts/dev/sample_tool.py"
_HREF = "../scripts/#script-dev-sample_tool-py"


def _resolve(text: str) -> str | None:
    return _HREF if text == _SAMPLE else None


def test_a_resolvable_span_is_wrapped_in_a_link() -> None:
    linked = hook.link_paths(f"<p>run <code>{_SAMPLE}</code> now</p>", _resolve)
    assert f'<a class="repo-path" href="{_HREF}">' in linked
    assert f"<code>{_SAMPLE}</code></a>" in linked


def test_an_unresolvable_span_is_untouched() -> None:
    original = "<p>see <code>ansible/deploy.yml</code></p>"
    assert hook.link_paths(original, _resolve) == original


@pytest.mark.parametrize(
    "template",
    [
        # A fenced block shows a command to run, not a file to go and read.
        "<pre><code>uv run {path} --tags docs</code></pre>",
        # Already a link: nesting an anchor inside one produces invalid HTML.
        '<p><a href="../deploying/"><code>{path}</code></a></p>',
        # A heading carries its own permalink, and the Usage headings on the Scripts page
        # would otherwise link two screens up the page they are already on.
        '<h3 id="sample-tool"><code>{path}</code></h3>',
        # The id says this cell IS the target; linking it would point it at itself.
        '<td><code id="script-dev-sample_tool-py">{path}</code></td>',
    ],
)
def test_a_protected_region_is_left_alone(template: str) -> None:
    html = template.format(path=_SAMPLE)
    assert hook.link_paths(html, _resolve) == html


def test_text_around_a_protected_region_is_still_linked() -> None:
    linked = hook.link_paths(
        f"<p><code>{_SAMPLE}</code></p>"
        f"<pre><code>{_SAMPLE}</code></pre>"
        f"<p><code>{_SAMPLE}</code></p>",
        _resolve,
    )
    assert linked.count('class="repo-path"') == 2
