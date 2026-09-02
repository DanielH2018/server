"""Guard: every relative markdown link on the docs site resolves to a real file.

Moving a document is what breaks these, and nothing else here catches it.
`test_adr_links.py` checks the ADR cross-reference graph, `test_documented_paths_exist.py`
checks repo paths named in prose, and `scripts/test_mkdocs_config.py` checks the nav — an
inline `[text](../thing.md)` inside a page falls through all three. A dead one renders as a
link that 404s on the built site, with nothing red anywhere.

That is not hypothetical. Regrouping `docs/archive/` into per-programme subdirectories needed
every relative link inside the moved files deepened by one level, and the sweep that did it
also deepened a link in `k3s-migration/`, which had not moved. Every existing gate stayed
green.

A **bare** same-directory link — `[text](sibling.md)`, no `./` — is the same defect and the
easier one to miss. The first version of this guard matched only paths starting with `.` and
passed two dead bare links straight through; `mkdocs build --strict` in CI is what caught them.
So the pattern below accepts any target that is not a URL, a fragment or an absolute path.

Clean/flagged pairs below, per the repo rule that a new check ships with a proof it can go RED.

Run: uv run pytest ansible/tests/repo/test_docs_relative_links_resolve.py
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from _helpers import REPO


# `[text](target)`, with the fragment stripped — an anchor is a heading, which this guard does
# not resolve. `!` before the bracket is allowed so an image counts too.
_LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]*?)(?:#[^)]*)?\)")

# A target this guard has no business resolving: an external URL, a mail link, a bare fragment,
# a template placeholder, or an absolute path (which mkdocs resolves against the docs root, not
# the filesystem, so `base / target` would be the wrong question).
_NOT_A_RELATIVE_PATH = re.compile(r"^(?:[a-z][a-z0-9+.-]*:|/|<|\{|$)", re.IGNORECASE)


def broken_links(text: str, base: Path) -> list[str]:
    """Relative targets in `text` that do not exist, resolved against directory `base`."""
    return [
        target
        for m in _LINK.finditer(text)
        if not _NOT_A_RELATIVE_PATH.match(target := m.group(1))
        and not (base / target).exists()
    ]


def site_pages() -> list[Path]:
    """Every markdown page mkdocs builds, archive included.

    Deliberately NOT `_helpers.discover_docs()`, which excludes `docs/archive/` — and the two
    dead links this guard was written for were both in there. mkdocs builds and serves the
    archive (`not_in_nav` keeps it out of the nav, not out of the site), so a link that 404s
    there 404s for a reader who followed one into it.

    `git ls-files` rather than rglob, for the reason `discover_docs` records: this repo grows a
    full working tree per live session under `.claude/worktrees/<name>/`, and an rglob would
    judge this commit against other sessions' checkouts.
    """
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--", "docs/**.md"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return sorted(REPO / rel for rel in listed.split("\0") if rel)


def test_every_relative_link_on_the_docs_site_resolves():
    pages = site_pages()
    assert pages, "no docs pages found — the discovery is broken, not the links"
    broken = {
        str(doc): links
        for doc in pages
        if (links := broken_links(doc.read_text(), doc.parent))
    }
    assert not broken, f"relative links pointing at nothing: {broken}"


def test_a_link_to_a_real_sibling_is_clean(tmp_path):
    (tmp_path / "sibling.md").write_text("")
    assert broken_links("see [it](./sibling.md)", tmp_path) == []


def test_a_link_to_a_missing_file_is_flagged(tmp_path):
    assert broken_links("see [it](./gone.md)", tmp_path) == ["./gone.md"]


def test_a_link_one_level_too_deep_is_flagged(tmp_path):
    """The exact shape a directory regroup introduces: a correct link deepened once too often."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "here.md").write_text("")
    (tmp_path / "docs" / "sub").mkdir()
    base = tmp_path / "docs" / "sub"
    assert broken_links("[x](../here.md)", base) == []
    assert broken_links("[x](../../here.md)", base) == ["../../here.md"]


def test_an_absolute_url_is_not_treated_as_a_path(tmp_path):
    assert broken_links("[x](https://example.com/a.md)", tmp_path) == []


def test_an_anchor_on_a_real_file_is_clean(tmp_path):
    (tmp_path / "sibling.md").write_text("")
    assert broken_links("[x](./sibling.md#a-heading)", tmp_path) == []


def test_a_bare_sibling_link_is_checked_in_both_directions(tmp_path):
    """No `./` prefix. Two of these survived the first version of this guard."""
    (tmp_path / "there.md").write_text("")
    assert broken_links("[x](there.md)", tmp_path) == []
    assert broken_links("[x](gone.md)", tmp_path) == ["gone.md"]


def test_an_image_target_is_checked_too(tmp_path):
    assert broken_links("![map](assets/map.svg)", tmp_path) == ["assets/map.svg"]


def test_a_bare_fragment_is_not_a_path(tmp_path):
    assert broken_links("[x](#a-heading)", tmp_path) == []


def test_a_mail_link_is_not_a_path(tmp_path):
    assert broken_links("[x](mailto:someone@example.com)", tmp_path) == []
