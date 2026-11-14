"""Each homepage tool tile's UI uses the tile's icon colour for its browser-tab favicon.

The tiles (`calendar-tool-*` ids) draw a coloured `mdi-<name>-#rrggbb` icon. Each UI carries
its own copy of that glyph as an SVG, so a recoloured tile leaves the tab icon behind unless
something compares the two.

Run: uv run pytest tests/services/test_homepage_tool_tile_favicons.py
"""

import re
from urllib.parse import unquote

import pytest
import yaml

from _homepage_config import services_yaml
from lib.repo_paths import REPO

DOCS_FAVICON = "assets/favicon.svg"

FAVICON_SOURCES = {
    "Deploy": REPO / "hosts/components/deploy-ui/files/deploy_ui.html",
    "Artifacts": REPO / "k8s/roles/artifacts/files/_gui_html.py",
    "Docs": REPO / "docs" / DOCS_FAVICON,
}

_FILL = re.compile(r"""fill=['"](#[0-9a-fA-F]{6})['"]""")


def _tiles(node):
    """Yields (name, conf) for every tile carrying an `id:`, at any group depth."""
    if isinstance(node, list):
        for item in node:
            yield from _tiles(item)
    elif isinstance(node, dict):
        for name, value in node.items():
            if isinstance(value, dict) and "id" in value:
                yield name, value
            else:
                yield from _tiles(value)


def tool_tile_colours() -> dict[str, str]:
    """Tile name -> the `#rrggbb` suffix of its mdi icon, for every `calendar-tool-*` tile."""
    return {
        name: conf["icon"].rsplit("-", 1)[1].lower()
        for name, conf in _tiles(services_yaml())
        if conf["id"].startswith("calendar-tool-")
    }


def fill_colours(text: str) -> set[str]:
    """Every SVG `fill` colour in a file, with a data URI's `%23` decoded to `#`."""
    return {c.lower() for c in _FILL.findall(unquote(text))}


def test_every_tool_tile_has_a_favicon_source():
    assert set(tool_tile_colours()) == set(FAVICON_SOURCES)


@pytest.mark.parametrize("tile", sorted(FAVICON_SOURCES))
def test_the_favicon_matches_the_tile_colour_is_clean(tile):
    colour = tool_tile_colours()[tile]
    assert colour in fill_colours(FAVICON_SOURCES[tile].read_text()), (
        f"{tile}'s favicon does not use its homepage tile colour {colour}"
    )


def test_a_favicon_in_another_colour_is_flagged():
    assert "#38bdf8" not in fill_colours("<path fill='%23fb923c' d='M0 0'/>")


def test_mkdocs_serves_the_docs_favicon():
    class _Loader(yaml.SafeLoader):
        pass

    _Loader.add_multi_constructor(
        "tag:yaml.org,2002:python/name:", lambda loader, suffix, node: suffix
    )
    _Loader.add_multi_constructor("!", lambda loader, suffix, node: suffix)
    config = yaml.load((REPO / "mkdocs.yml").read_text(), Loader=_Loader)
    assert config["theme"]["favicon"] == DOCS_FAVICON
