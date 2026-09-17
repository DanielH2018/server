"""The docs site's nginx sends `Cache-Control: no-cache` on the pages and assets it serves.

Nothing this repo writes into the docs tree is content-hashed, so a browser or an edge that
caches a script past a rebuild runs the old bundle against the new page. `no-cache` is
revalidate-before-use — an unchanged file still answers 304 with no body — and it sits at
`server` level so `location /` inherits it. nginx's `add_header` does not merge across levels:
a location declaring ANY `add_header` of its own drops every inherited one, which is why the
`/build-info.json` location restates its policy and why `location /` must declare none. Both
halves are asserted, since either one going away serves stale assets behind a healthy pod.

Run: uv run pytest ansible/tests/services/test_docs_nginx_revalidates_assets.py
"""

import re

from _k8s_render import rendered_docs

NO_CACHE = 'add_header Cache-Control "no-cache";'


def _server_conf() -> str:
    for role, _tpl, doc in rendered_docs():
        if (
            role == "docs"
            and doc.get("kind") == "ConfigMap"
            and "default.conf" in doc.get("data", {})
        ):
            return doc["data"]["default.conf"]
    raise AssertionError("the docs role renders no ConfigMap carrying default.conf")


def _location_root_body(conf: str) -> str:
    match = re.search(r"location / \{(.*?)\n\s*\}", conf, re.DOTALL)
    assert match, "no `location /` block"
    return match.group(1)


def effective_asset_cache_control(conf: str) -> str | None:
    """The Cache-Control `location /` answers with, under nginx's no-merge rule."""
    location = _location_root_body(conf)
    if "add_header" in location:
        found = re.search(r'add_header Cache-Control "([^"]*)"', location)
        return found.group(1) if found else None
    outside_locations = re.sub(r"location [^{]*\{.*?\n\s*\}", "", conf, flags=re.DOTALL)
    found = re.search(r'add_header Cache-Control "([^"]*)"', outside_locations)
    return found.group(1) if found else None


def test_pages_and_assets_are_revalidated_before_use():
    conf = _server_conf()
    assert NO_CACHE in conf
    assert effective_asset_cache_control(conf) == "no-cache"


def test_a_location_level_header_drops_the_inherited_policy():
    base = (
        'server {\n    add_header Cache-Control "no-cache";\n'
        "    location = /healthz {\n        return 200;\n    }\n"
        "    location / {\n        try_files $uri =404;\n    }\n}\n"
    )
    assert effective_asset_cache_control(base) == "no-cache"
    shadowed = base.replace(
        "try_files", "add_header X-Frame-Options DENY;\n        try_files"
    )
    assert effective_asset_cache_control(shadowed) is None
    assert effective_asset_cache_control(base.replace(NO_CACHE, "")) is None
