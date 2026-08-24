"""mkdocs.yml navigation must point only at documents that exist.

A nav entry naming a missing file makes `mkdocs build --strict` fail, but that
failure arrives late and reads as a build error rather than a broken link. This
asserts the same property directly against the tree.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
MKDOCS = REPO / "mkdocs.yml"


def _nav_paths(nav: object) -> list[str]:
    """Every document path in a mkdocs `nav:` tree, at any nesting depth."""
    found: list[str] = []
    if isinstance(nav, str):
        found.append(nav)
    elif isinstance(nav, list):
        for entry in nav:
            found.extend(_nav_paths(entry))
    elif isinstance(nav, dict):
        for value in nav.values():
            found.extend(_nav_paths(value))
    return found


def _load_config() -> dict:
    # MkDocs uses `!!python/name:` tags for extensions, which safe_load rejects.
    # The nav needs none of them, so unknown tags are read as plain scalars.
    class _Loader(yaml.SafeLoader):
        pass

    _Loader.add_multi_constructor(
        "tag:yaml.org,2002:python/name:", lambda loader, suffix, node: suffix
    )
    _Loader.add_multi_constructor("!", lambda loader, suffix, node: suffix)
    return yaml.load(MKDOCS.read_text(), Loader=_Loader)


def test_every_nav_entry_resolves_to_a_file():
    config = _load_config()
    docs_dir = REPO / config.get("docs_dir", "docs")
    missing = [p for p in _nav_paths(config["nav"]) if not (docs_dir / p).is_file()]
    assert not missing, f"nav entries with no file: {missing}"


def test_nav_covers_every_toplevel_doc():
    """Every docs/*.md is reachable from the nav.

    A document absent from the nav is still built and still served, but nothing
    links to it — which is indistinguishable from it not existing.
    """
    config = _load_config()
    docs_dir = REPO / config.get("docs_dir", "docs")
    listed = set(_nav_paths(config["nav"]))
    on_disk = {p.name for p in docs_dir.glob("*.md")}
    assert not (on_disk - listed), (
        f"docs/*.md missing from nav: {sorted(on_disk - listed)}"
    )
