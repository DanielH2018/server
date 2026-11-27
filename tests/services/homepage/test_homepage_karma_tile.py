"""The Karma tile counts the alerts Karma shows.

Karma's browser UI applies `filters.default` from its config, and its `/alerts.json` API
applies only the filters a request sends. So each of the tile's requests repeats the default
list, and a filter added to Karma alone would make the tile count alerts Karma hides.

Run: uv run pytest tests/services/homepage/test_homepage_karma_tile.py
"""

from lib import yaml_fast
from _homepage_config import custom_css, tiles
from _k8s_render import render_role_template


def _karma_default_filters() -> list[str]:
    (configmap,) = [
        d
        for d in yaml_fast.safe_load_all(
            render_role_template("karma", "configmap.yaml.j2")
        )
        if d
    ]
    return yaml_fast.safe_load(configmap["data"]["karma.yaml"])["filters"]["default"]


def _karma_tile_widgets() -> list[dict]:
    (body,) = [body for name, body in tiles() if name == "Karma"]
    return body["widgets"]


def unshared_filters(widget: dict, defaults: list[str]) -> list[str]:
    """Filters on one side only, once the widget's own `severity=` filter is set aside."""
    sent = [
        f for f in widget["requestBody"]["filters"] if not f.startswith("severity=")
    ]
    return sorted(set(sent) ^ set(defaults))


def test_each_tile_request_sends_karmas_default_filters():
    # fact: k8s/roles/homepage/CLAUDE.md#Notable
    defaults = _karma_default_filters()
    widgets = _karma_tile_widgets()
    assert len(widgets) == 2, widgets
    for widget in widgets:
        assert unshared_filters(widget, defaults) == [], widget["mappings"][0]["label"]


def test_the_karma_tile_names_its_blocks_in_config():
    """customapi takes a `label:` per block, so no CSS rule renames the tile's headings."""
    labels = [w["mappings"][0]["label"] for w in _karma_tile_widgets()]
    assert labels == ["Critical", "Warning"]
    assert "karma." not in custom_css()


def test_a_default_filter_the_tile_omits_is_flagged():
    (widget, _) = _karma_tile_widgets()
    assert unshared_filters(widget, [*_karma_default_filters(), "@silenced=false"]) == [
        "@silenced=false"
    ]
