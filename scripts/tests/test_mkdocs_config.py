"""mkdocs.yml navigation must point only at documents that exist.

A nav entry naming a missing file makes `mkdocs build --strict` fail, but that
failure arrives late and reads as a build error rather than a broken link. This
asserts the same property directly against the tree.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import yaml

REPO = Path(__file__).resolve().parents[2]
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


def _is_external(value: str) -> bool:
    """A nav entry that is a link off the site rather than a document in the tree.

    MkDocs passes an absolute URL through as a nav heading that is only a link. The
    artifacts browser is one, and its host is a sentinel resolved in the browser --
    see mkdocs.yml and docs/assets/fqdn-links.js.
    """
    return value.startswith(("http://", "https://"))


def test_every_nav_entry_resolves_to_a_file():
    config = _load_config()
    docs_dir = REPO / config.get("docs_dir", "docs")
    missing = [
        p
        for p in _nav_paths(config["nav"])
        if not _is_external(p) and not (docs_dir / p).is_file()
    ]
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


def _patterns(config, key):
    raw = config.get(key) or ""
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _exclude_patterns(config):
    """Both ways a page can be accounted for without a nav entry.

    `exclude_docs` means "do not build it"; `not_in_nav` means "build and serve it, but do
    not link it". They are different decisions and mkdocs treats them differently — what
    matters to this test is only that SOMEONE made one of them.
    """
    return _patterns(config, "exclude_docs") + _patterns(config, "not_in_nav")


def _excluded(rel: str, patterns) -> bool:
    return any(
        rel == pat or rel.startswith(pat) if pat.endswith("/") else rel == pat
        for pat in patterns
    )


def test_every_doc_is_either_in_the_nav_or_explicitly_excluded():
    """The whole tree, not just the top level.

    `test_nav_covers_every_toplevel_doc` globs `*.md` — one directory deep — which is the
    scope of the change it shipped with. Everything in a SUBDIRECTORY was outside it, and
    mkdocs builds and serves those pages regardless of the nav, logging an unlinked page at
    INFO so `--strict` does not fail either (2026-08-25 review M-7).

    A page must therefore be one of two things on purpose: linked, or named in
    `exclude_docs`. Silence is no longer an option.

    Note what this can and cannot bind. It walks the CHECKOUT, so the 11 gitignored
    `superpowers/` pages are absent in CI and this passes over them vacuously -- their
    protection is the `exclude_docs` entry itself, which applies wherever the site is built
    from, including the host working tree the cron uses. This test keeps that entry honest
    for everything tracked.
    """
    config = _load_config()
    docs_dir = REPO / config.get("docs_dir", "docs")
    listed = set(_nav_paths(config["nav"]))
    patterns = _exclude_patterns(config)

    unreachable = sorted(
        rel
        for p in docs_dir.rglob("*.md")
        if (rel := p.relative_to(docs_dir).as_posix()) not in listed
        and not _excluded(rel, patterns)
    )
    assert not unreachable, (
        "these pages are built and served but neither linked from the nav nor named in "
        "exclude_docs, so nothing points at them and nothing decided they should exist: "
        f"{unreachable}"
    )


def test_the_untracked_plans_directory_is_excluded():
    """Untracked is not unpublished. The docs cron builds from the host's working tree, so
    the gitignored `docs/superpowers/` plans were served from an internet-facing site.
    Pinned by name because the directory does not exist in a fresh checkout, which is
    exactly why the test above cannot see it.
    """
    assert "superpowers/" in _patterns(_load_config(), "exclude_docs"), (
        "docs/superpowers/ is gitignored but still built from the host's working tree"
    )


def test_an_external_nav_entry_uses_the_sentinel_host():
    """A real domain typed into the nav would work on one tier and break on the other.

    `domain` is SOPS-sourced, so a static nav cannot name it. Every off-site entry
    carries `<service>.local.invalid` and is resolved in the browser instead.
    """
    external = [p for p in _nav_paths(_load_config()["nav"]) if _is_external(p)]
    baked = [
        u
        for u in external
        if urlparse(u).hostname.split(".")[-2:] != ["local", "invalid"]
    ]
    assert not baked, f"external nav entries with a baked-in domain: {baked}"
