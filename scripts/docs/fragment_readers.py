"""The readers behind the doc fragments: the tree, parsed, never imported.

`scripts/docs/gen_doc_fragments.py` assembles the fragments; this module is the half that
touches the tree. Each function takes a path or a blob of text and returns plain data, so a
renderer in `fragment_renderers.py` is a pure function of what one of these returns and
neither half has to know about the other.

STATIC PARSING ONLY, for the reason the generator's own docstring gives.
Python constants come from `ast`, package listings from `yaml_fast`, and the
fail2ban jail template from `configparser`. The rotation registry is the generator's to
read, through `rotation_tools.load_registry`.
"""

import ast
import configparser
from collections.abc import Sequence
from pathlib import Path as _Path

from lib import yaml_fast
from lib.repo_paths import REPO


def module_constant(path: _Path, name: str):
    """The literal a module assigns to `name` at top level, without importing the module."""
    tree = ast.parse(path.read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise KeyError(f"{path.relative_to(REPO)}: no top-level `{name} = <literal>`")


def hosts_listing_component(
    component: str, hosts_dir: _Path | None = None
) -> list[str]:
    """Every host whose `hosts/<host>/package.yml` lists `component`, sorted by host."""
    hosts_dir = hosts_dir or REPO / "hosts"
    return sorted(
        path.parent.name
        for path in hosts_dir.glob("*/package.yml")
        if component
        in (yaml_fast.safe_load(path.read_text()) or {}).get("components", [])
    )


def parse_jails(conf: str) -> list[dict[str, str]]:
    """Every enabled jail with its effective maxretry, findtime and bantime.

    The template is plain INI, so configparser reads it and resolves each jail's values
    through `[DEFAULT]` the way fail2ban does.
    """
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(conf)
    jails = []
    for name in parser.sections():
        section = parser[name]
        if section.get("enabled", "false").lower() != "true":
            continue
        jails.append(
            {
                "jail": name,
                "maxretry": section["maxretry"],
                "findtime": section["findtime"],
                "bantime": section["bantime"],
            }
        )
    return jails


def container_udp_port(entries: Sequence[dict], name: str) -> str:
    entries = [c for c in entries if c["name"] == name]
    assert len(entries) == 1, (
        f"expected one {name!r} in the host's service entries, found {len(entries)}"
    )
    return str(entries[0]["udp_port"])
