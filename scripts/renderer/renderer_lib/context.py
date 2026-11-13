"""The variables one k8s role's templates render with, built from the git tree alone.

`render_context` layers role defaults, `group_vars/all.yml` and host_vars in Ansible's
precedence, and adds `services` and `hosts`. This module adds two things on top:

- `services` is the toposorted view: `cluster/services.yml` sorted with `toposort_containers`,
  and templates read that order (homepage tiles, scrape jobs).
- The stub file's variables go over every layer, as `-e @FILE` extra vars do.

Typical usage example:

    ctx = template_context(K8S_ROLES / "littlelink", load_stubs(Path("/tmp/r1-stubs.yml")))
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from pathlib import Path

from lib import yaml_fast
from lib.inventory_lists import K8S_HOST
from lib.estate import REPO_INVENTORY, Inventory
from lib.render_context import render_context
from lib.render_guard import StrictChainableUndefined
from lib.repo_paths import K8S_ROLES


from renderer.template_filters.toposort import build_k8s_dep_map, toposort_containers


# Host_vars keys only a host component reads; each reads a name only that component defines.
HOST_COMPONENT_VARS = frozenset({"claude_code_watch_uids"})


def load_stubs(path: Path) -> dict:
    """The variables of a stub file that `shadow_diff.py stubs` wrote."""
    return yaml_fast.safe_load(path.read_text()) or {}


def sorted_services(entries: list[dict]) -> list[dict]:
    """`entries` in the order the templates read them, toposorted."""
    return toposort_containers(entries, build_k8s_dep_map(entries, str(K8S_ROLES)))


def template_context(
    role_dir: Path,
    stubs: dict,
    host: str = K8S_HOST,
    inventory: Inventory = REPO_INVENTORY,
) -> dict:
    """The context `role_dir`'s templates render with on `host`.

    Args:
        role_dir: The role directory, `k8s/roles/<tag>`.
        stubs: The stub file's variables, laid over every layer.
        host: The inventory host the role renders for.
        inventory: Where the inventory files live; the repo's own unless a test says not.
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
    # The sorted view goes in as an override, above every file layer, so a default that reads
    # `services` resolves against the view too. It sorts the resolved entries: an entry's
    # value may be Jinja (nut's `ups` param).
    return resolve({**stubs, "services": sorted_services(first["services"])})
