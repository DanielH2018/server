"""The readers behind the doc fragments: the tree, parsed, never imported.

`scripts/docs/gen_doc_fragments.py` assembles the fragments; this module is the half that
touches the tree. Each function takes a path or a blob of text and returns plain data, so a
renderer in `fragment_renderers.py` is a pure function of what one of these returns and
neither half has to know about the other.

STATIC PARSING ONLY, for the reason the generator's own docstring gives: importing the
deployer or the rotation tool would bootstrap `sys.path` and read the environment on import.
Python constants come from `ast`, role defaults and inventory from `yaml.safe_load`, and the
fail2ban jail template from `configparser`.
"""

import ast
import configparser
import sys as _sys
from pathlib import Path as _Path


# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

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


def role_defaults(path: _Path) -> dict:
    return yaml_fast.safe_load(path.read_text())


def host_has_docker(
    hosts_ini: _Path, group_vars: dict, host_vars_dir: _Path
) -> dict[str, bool]:
    """`{host: has_docker}` for every host in hosts.ini's `[homeservers]`, in file order.

    A host's own host_vars value wins over group_vars, the precedence Ansible applies. The
    host list is what `initial_setup.yml` targets, so it is the set of hosts that install the
    crons this role pings from.
    """
    hosts, in_group = [], False
    for raw in hosts_ini.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if line.startswith("["):
            in_group = line == "[homeservers]"
        elif line and in_group:
            hosts.append(line.split()[0])
    default = bool(group_vars.get("has_docker", False))
    out = {}
    for host in hosts:
        path = host_vars_dir / f"{host}.yml"
        own = yaml_fast.safe_load(path.read_text()) if path.is_file() else {}
        out[host] = bool((own or {}).get("has_docker", default))
    return out


def registry_counts(path: _Path) -> dict[str, int]:
    """How many registered secrets sit in each tier."""
    counts: dict[str, int] = {}
    for entry in yaml_fast.safe_load(path.read_text())["entries"].values():
        tier = str(entry.get("tier", "?"))
        counts[tier] = counts.get(tier, 0) + 1
    return counts


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


def container_udp_port(host_vars: dict, name: str) -> str:
    entries = [c for c in host_vars["containers_list"] if c.get("name") == name]
    assert len(entries) == 1, (
        f"expected one {name!r} in containers_list, found {len(entries)}"
    )
    return str(entries[0]["udp_port"])
