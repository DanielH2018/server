"""homepage's app config as the POD receives it, shared by the guards over it.

Every homepage config file — `services.yaml`, `settings.yaml`, `widgets.yaml`, `custom.css` —
reaches the pod as one key of the `homepage-config` Secret that `config-secret.yaml.j2` renders.
The guards in this directory used to read `templates/config/*.j2` as text instead, and each one
paid the same price: a Jinja template is not loadable YAML, so every guard hand-rolled a line
scanner and pinned source literals (`{{ domain }}`, `{{ k8s_namespace }}`) that say nothing
about what the pod ends up with. Rendered, the same files are ordinary YAML and CSS, so a guard
asserts on the parsed document and a `fields:` list is a Python list rather than a regex group.

The render itself comes from `_k8s_render.rendered_texts()`, which the whole suite shares — so
reading the config here costs a YAML parse, not a second render of the tree.

Kept as a `_`-prefixed sibling for the same reason `_kuma_entities.py` is one: pytest prepends a
test's own directory to `sys.path`, so each guard reaches this by bare name without it being
collected as a test itself.
"""

from functools import lru_cache

from lib import yaml_fast
from _k8s_render import host_context, rendered_texts

CONFIG_SECRET = "config-secret.yaml.j2"


@lru_cache(maxsize=1)
def config_files() -> dict[str, str]:
    """Secret key -> file body, for every config file homepage mounts.

    Raises:
        AssertionError: the role no longer renders exactly one `config-secret.yaml.j2`. The
            guards built on this all assert over the files it yields, so an empty mapping
            would pass every one of them while checking nothing.
    """
    found = [
        text
        for role, template, text in rendered_texts()
        if role == "homepage" and template == CONFIG_SECRET
    ]
    assert len(found) == 1, (
        f"expected one rendered homepage/{CONFIG_SECRET}, got {len(found)} — "
        "every guard reading homepage's config asserts over an empty mapping without it"
    )
    secret = yaml_fast.safe_load(found[0])
    assert secret.get("kind") == "Secret", (
        f"homepage/{CONFIG_SECRET} no longer renders a Secret, got {secret.get('kind')!r}"
    )
    return secret["stringData"]


@lru_cache(maxsize=1)
def services_yaml() -> list:
    """The parsed tile list: a list of `{group name: members}` mappings."""
    return yaml_fast.safe_load(config_files()["services.yaml"])


@lru_cache(maxsize=1)
def settings_yaml() -> dict:
    """The parsed `settings.yaml`, which holds `layout:` and the widget providers."""
    return yaml_fast.safe_load(config_files()["settings.yaml"])


@lru_cache(maxsize=1)
def widgets_yaml() -> list:
    """The parsed `widgets.yaml`: the information-widget list, one `{type: options}` each."""
    return yaml_fast.safe_load(config_files()["widgets.yaml"])


def custom_css() -> str:
    """`custom.css` as the pod serves it — plain CSS, with no Jinja left in it."""
    return config_files()["custom.css"]


def namespace() -> str:
    """`k8s_namespace` at the value the render used, for a guard matching a ClusterIP host."""
    return host_context()["k8s_namespace"]


def config_urls(node=None) -> set[str]:
    """Every `url` value anywhere in the tile list, however deeply nested.

    Walks the parsed document rather than scanning for `url:` lines, so the calendar widget's
    `integrations[].url` is reached the same way a widget's own `url:` is. Two guards need this
    — one checks what the URLs carry, the other which Services they dial — and a second copy
    would let the two censuses drift apart.
    """
    if node is None:
        node = services_yaml()
    found: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "url" and isinstance(value, str):
                found.add(value)
            else:
                found |= config_urls(value)
    elif isinstance(node, list):
        for item in node:
            found |= config_urls(item)
    return found


def tiles(groups: list | None = None) -> list[tuple[str, dict]]:
    """(tile name, tile body) for every tile in the list, groups flattened.

    homepage nests groups: `Top Row` holds `Services` and `Calendar`, each itself a list of
    tiles, and a tile body is the one mapping under the tile's name. Recursing rather than
    scanning the top level keeps a guard from silently covering only the ungrouped tiles —
    which is where almost every widget on this dashboard lives.
    """
    out: list[tuple[str, dict]] = []
    for entry in services_yaml() if groups is None else groups:
        for name, body in entry.items():
            if isinstance(body, list):
                out.extend(tiles(body))
            elif isinstance(body, dict):
                out.append((name, body))
    return out
