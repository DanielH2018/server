"""The variables one k8s role's templates render with, built from the git tree alone.

`render_context` layers role defaults, `group_vars/all.yml` and host_vars in Ansible's
precedence. Three things the templates read are not in those files, and this module supplies
them:

- `containers_list` is the k8s view: the host's list filtered with `filter_by_platform('k8s')`
  and sorted with `toposort_containers`, and templates read that order (homepage tiles,
  scrape jobs).
- `hostvars` holds every inventory host. The rendered host's entry is the rendered context
  itself, so `hostvars[k8s_primary_node].containers_list` is the k8s view. Every other host
  carries its `hosts.ini` inline variables and its own host_vars, resolved against that host's
  own variables, so a daniel-pi value that reads `server_ip` is daniel-pi's address.
- The stub file's variables go over every layer, as `-e @FILE` extra vars do.

Typical usage example:

    ctx = template_context(K8S_ROLES / "littlelink", load_stubs(Path("/tmp/r1-stubs.yml")))
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from pathlib import Path

import jinja2

from lib import yaml_fast
from lib.ansible_inventory import K8S_HOST, inventory_hosts
from lib.estate import REPO_INVENTORY, Inventory
from lib.k8s_context import resolve_vars
from lib.render_context import render_context
from lib.render_guard import StrictChainableUndefined, load_yaml
from lib.repo_paths import ANSIBLE, FILTER_PLUGINS

# Ansible loads a filter plugin by file path; a script reaches it through this insert.
if str(FILTER_PLUGINS) not in _sys.path:
    _sys.path.insert(0, str(FILTER_PLUGINS))

from toposort import build_k8s_dep_map, filter_by_platform, toposort_containers


# Host_vars keys only a host component reads; each reads a name only that component defines.
HOST_COMPONENT_VARS = frozenset({"claude_code_watch_uids"})


def load_stubs(path: Path) -> dict:
    """The variables of a stub file that `shadow_diff.py stubs` wrote."""
    return yaml_fast.safe_load(path.read_text()) or {}


def k8s_containers_list(entries: list[dict]) -> list[dict]:
    """`entries` as the templates read them: k8s entries only, toposorted.

    The dependency map is built from the filtered list, so a non-k8s entry adds no edge.
    """
    k8s = filter_by_platform(entries, "k8s")
    return toposort_containers(k8s, build_k8s_dep_map(k8s, str(ANSIBLE)))


def template_context(
    role_dir: Path,
    stubs: dict,
    host: str = K8S_HOST,
    inventory: Inventory = REPO_INVENTORY,
) -> dict:
    """The context `role_dir`'s templates render with on `host`.

    Args:
        role_dir: The role directory, `ansible/roles/k8s/<tag>`.
        stubs: The stub file's variables, laid over every layer.
        host: The inventory host the role renders for.
        inventory: Where the inventory files live; the repo's own unless a test says not.

    Raises:
        ValueError: Another host's `hostvars` entry will not resolve.
    """

    # No BASE_CONTEXT: its stubs would fill in a name no file holds. Under the strict class a
    # variable whose value names an undefined one is left undefined rather than resolved to
    # STUB or None, so a template that reads it stops the render. Not `strict=True`: a built-image
    # pin reads `k8s_built_image_tags`, which `task_inputs` derives per role after this layer.

    def resolve(overrides: dict) -> dict:
        return render_context(
            role_dir,
            host,
            overrides,
            inventory=inventory,
            base={},
            undefined_cls=StrictChainableUndefined,
            withhold=HOST_COMPONENT_VARS,
        )

    first = resolve(stubs)
    # The k8s view goes in as an override, above every file layer, so a default that reads
    # `containers_list` resolves against the view too.
    view = k8s_containers_list(first["containers_list"])
    ctx = resolve({**stubs, "containers_list": view})
    ctx["hostvars"] = inventory_hostvars(ctx, host, stubs, inventory)
    return ctx


def inventory_hostvars(
    ctx: dict, host: str, stubs: dict, inventory: Inventory = REPO_INVENTORY
) -> dict[str, dict]:
    """`hostvars` for every host in `hosts.ini`, with `host`'s entry taken from `ctx`.

    `host`'s entry is `ctx` without its own `hostvars` key, which no template reads and which
    would make the context hold itself. Another host's entry holds its `hosts.ini` inline
    variables under its own host_vars, Ansible's precedence for the two. Each value resolves
    against `group_vars/all.yml`, those two layers and the stubs.

    Raises:
        ValueError: A host's value names an undefined variable, or still holds Jinja after
            resolution.
    """
    group = load_yaml(inventory.all_vars)
    hostvars = {}
    for entry in inventory_hosts(inventory.hosts_ini):
        if entry.name == host:
            hostvars[host] = {k: v for k, v in ctx.items() if k != "hostvars"}
            continue
        own = {**entry.settings, **load_yaml(inventory.host_vars / f"{entry.name}.yml")}
        try:
            resolved = resolve_vars(
                own,
                {**group, **own, **stubs},
                undefined_cls=StrictChainableUndefined,
            )
        except jinja2.TemplateError as exc:
            raise ValueError(f"hostvars[{entry.name!r}]: {exc}") from exc
        unresolved = sorted(k for k, v in resolved.items() if "{{" in str(v))
        if unresolved:
            raise ValueError(f"hostvars[{entry.name!r}]: {unresolved} will not expand")
        hostvars[entry.name] = resolved
    return hostvars
