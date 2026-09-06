#!/usr/bin/env python3
"""Homepage splits the longhorn widget across two config files, and the wrong half is silent.

`providers.longhorn.url` in `settings.yaml.j2` holds the connection; the `- longhorn:` entry in
`widgets.yaml.j2` holds only the display options. A `url:` written beside those options is IGNORED:
homepage logs `<longhorn> Missing Longhorn URL` on every refresh, the tile renders empty, and the
Deployment stays 1/1 throughout. PR #1391 shipped exactly that, and nothing caught it — the config
is valid YAML, the manifests render, and `probe.py health homepage` exits 0.

Same shape as `test_headlamp_widget_mapping_order.py`: a homepage widget whose config is accepted
by every mechanical check while meaning something other than it reads.

Paired, per the repo's red-proof rule.

Run: uv run pytest ansible/tests/services/test_homepage_longhorn_widget_url.py
"""

from _helpers import ANSIBLE

CONFIG = ANSIBLE / "roles" / "k8s" / "homepage" / "templates" / "config"
SETTINGS = CONFIG / "settings.yaml.j2"
WIDGETS = CONFIG / "widgets.yaml.j2"

# The Service the URL must name. `longhorn-backend` is fenced by Longhorn's own chart-owned
# NetworkPolicy and would render an empty tile just as quietly.
FRONTEND = "longhorn-frontend.longhorn-system.svc.cluster.local"


def longhorn_url_lines(text: str) -> list[str]:
    """Every non-comment line assigning a `url:` inside a longhorn block."""
    out, inside = [], False
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.lstrip("- ").startswith("longhorn:"):
            inside = True
            continue
        if inside:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if not line.startswith((" ", "\t")):
                inside = False
                continue
            if stripped.startswith("url:"):
                out.append(stripped)
    return out


def test_settings_carries_the_longhorn_url():
    """The accepting half: the connection lives where homepage actually reads it."""
    urls = longhorn_url_lines(SETTINGS.read_text())
    assert len(urls) == 1, f"expected one url under providers.longhorn, got {urls}"
    assert FRONTEND in urls[0]


def test_widgets_carries_no_longhorn_url():
    """A url here is accepted by every check and read by nothing."""
    assert longhorn_url_lines(WIDGETS.read_text()) == []


def test_a_url_beside_the_display_options_is_found(tmp_path):
    """The rejecting half. A parser that found nothing would pass both tests above."""
    fixture = "\n".join(
        [
            "- longhorn:",
            "    url: http://longhorn-frontend.longhorn-system.svc.cluster.local",
            "    expanded: true",
            "",
            "- openmeteo:",
            "    url: http://elsewhere",
        ]
    )
    assert longhorn_url_lines(fixture) == [
        "url: http://longhorn-frontend.longhorn-system.svc.cluster.local"
    ]


def test_a_commented_out_url_is_not_counted():
    """The comment blocks in both files DISCUSS the url; a textual grep would match them."""
    fixture = "\n".join(
        ["- longhorn:", "    # url: http://ignored", "    expanded: true"]
    )
    assert longhorn_url_lines(fixture) == []
