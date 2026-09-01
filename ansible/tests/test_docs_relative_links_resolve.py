"""Guard: every relative markdown link in an operator doc resolves to a real file.

Moving a document is what breaks these, and nothing else here catches it.
`test_adr_links.py` checks the ADR cross-reference graph, `test_documented_paths_exist.py`
checks repo paths named in prose, and `scripts/test_mkdocs_config.py` checks the nav — an
inline `[text](../thing.md)` inside a page falls through all three. A dead one renders as a
link that 404s on the built site, with nothing red anywhere.

That is not hypothetical. Regrouping `docs/archive/` into per-programme subdirectories needed
every relative link inside the moved files deepened by one level, and the sweep that did it
also deepened a link in `k3s-migration/`, which had not moved. Every existing gate stayed
green.

Clean/flagged pairs below, per the repo rule that a new check ships with a proof it can go RED.

Run: uv run pytest ansible/tests/test_docs_relative_links_resolve.py
"""

from __future__ import annotations

import re
from pathlib import Path

from _helpers import discover_docs

# `[text](path)` where path is relative and not a URL. The fragment is stripped: an anchor is a
# heading, which this guard does not resolve.
_LINK = re.compile(r"\[[^\]]*\]\((\.\.?/[^)\s#]*)(?:#[^)]*)?\)")


def broken_links(text: str, base: Path) -> list[str]:
    """Relative targets in `text` that do not exist, resolved against directory `base`."""
    return [
        m.group(1) for m in _LINK.finditer(text) if not (base / m.group(1)).exists()
    ]


def test_every_relative_link_in_an_operator_doc_resolves():
    broken = {
        str(doc): links
        for doc in discover_docs()
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
