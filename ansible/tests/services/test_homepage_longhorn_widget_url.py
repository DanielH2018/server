#!/usr/bin/env python3
"""Homepage splits the longhorn widget across two config files, and the wrong half is silent.

`providers.longhorn.url` in `settings.yaml` holds the connection; the `- longhorn:` entry in
`widgets.yaml` holds only the display options. A `url:` written beside those options is IGNORED:
homepage logs `<longhorn> Missing Longhorn URL` on every refresh, the tile renders empty, and the
Deployment stays 1/1 throughout. PR #1391 shipped exactly that, and nothing caught it — the config
is valid YAML, the manifests render, and `probe.py health homepage` exits 0.

Same shape as `test_headlamp_widget_mapping_order.py`: a homepage widget whose config is accepted
by every mechanical check while meaning something other than it reads.

Reads both files RENDERED out of homepage's config Secret (`_homepage_config`). The previous form
scanned `settings.yaml.j2` and `widgets.yaml.j2` line by line, with its own indentation tracking
and its own comment skipping, because a Jinja template is not loadable YAML. Rendered, the two
files are ordinary mappings: `providers.longhorn` is a dict lookup and a commented-out `url:`
cannot reach the parse at all, so the two parser cases that needed their own tests are gone.

Paired, per the repo's red-proof rule.

Run: uv run pytest ansible/tests/services/test_homepage_longhorn_widget_url.py
"""

from _homepage_config import settings_yaml, widgets_yaml

# The Service the URL must name. `longhorn-backend` is fenced by Longhorn's own chart-owned
# NetworkPolicy and would render an empty tile just as quietly.
FRONTEND = "longhorn-frontend.longhorn-system.svc.cluster.local"


def widget_options(widgets: list, kind: str) -> list[dict]:
    """The options mapping of every `widgets.yaml` entry of type `kind`."""
    return [
        options
        for entry in widgets
        for name, options in entry.items()
        if name == kind and isinstance(options, dict)
    ]


def test_settings_carries_the_longhorn_url():
    """The accepting half: the connection lives where homepage actually reads it."""
    providers = settings_yaml().get("providers") or {}
    longhorn = providers.get("longhorn")
    assert isinstance(longhorn, dict), (
        f"settings.yaml has no providers.longhorn mapping, got {longhorn!r} — "
        "the tile would log 'Missing Longhorn URL' on every refresh"
    )
    assert FRONTEND in longhorn.get("url", "")


def test_widgets_carries_no_longhorn_url():
    """A url here is accepted by every check and read by nothing."""
    options = widget_options(widgets_yaml(), "longhorn")
    # Non-vacuity: the assertion below passes just as well if the widget is gone entirely.
    assert len(options) == 1, f"expected one longhorn widget entry, got {options}"
    assert "url" not in options[0], (
        f"widgets.yaml carries a longhorn url ({options[0]['url']!r}), which homepage ignores "
        "— the connection belongs under providers.longhorn in settings.yaml"
    )


def test_a_url_beside_the_display_options_is_found():
    """The rejecting half. A reader that found nothing would pass both tests above."""
    widgets = [
        {"longhorn": {"url": f"http://{FRONTEND}", "expanded": True}},
        {"openmeteo": {"url": "http://elsewhere"}},
    ]
    assert widget_options(widgets, "longhorn") == [
        {"url": f"http://{FRONTEND}", "expanded": True}
    ]
