#!/usr/bin/env python3
"""The inventory as the docs generators read it: hosts, each host's vars, and its entries.

WHY ONE LOADER. Before #3899 every generator under ``scripts/docs/`` found and parsed the
inventory itself. ``gen_doc_fragments`` read ``all.yml`` and two host_vars files through a
helper named ``role_defaults``, ``fragment_readers.host_has_docker`` and
``reference/hosts._flag`` each re-implemented host-over-group precedence, and
``gen_role_glance``, ``glance_facts``, ``service_catalog`` and ``reference/networking``
each walked the service list through a different helper. ``Estate`` is that reading
done once; a generator asks it for values and only renders.

THE PRECEDENCE RULE LIVES HERE, ONCE. ``inventory_layers`` is the order Ansible applies,
role defaults < ``group_vars/all.yml`` < host_vars. ``render_context`` builds its render
context on top of it, adding the stubs and the Jinja resolution a template render needs.
The docs generators take the layers RAW: a page prints what the inventory says, and an
expanded or ``STUB``-ed value would be a value no file holds.

WHERE IT READS. An ``Inventory`` names the files, defaulting to the repo's own. A test
points one at a ``tmp_path`` tree; nothing here reads a module constant past it.

Typical usage example:

    estate = Estate()
    estate.vars("daniel-pi")["has_docker"]  # True
    estate.source("daniel-pi", "has_docker")  # "host": daniel-pi's own file declares it
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Literal

from lib.inventory_lists import K8S_HOST
from lib.render_guard import host_files, host_layer, k8s_services, load_yaml
from lib.repo_paths import ALL_VARS, HOST_VARS, K8S_SERVICES

__all__ = [
    "PLANE_HOSTS",
    "REPO_INVENTORY",
    "Estate",
    "Inventory",
    "inventory_layers",
    "role_defaults",
]

# The host whose host_vars a plane's templates render with when the caller names none. Every
# k8s role renders with daniel-box's host_vars, the group_vars `k8s_primary_node`.
# A setup role runs on several hosts and a Pi compose role on the host that lists it, so
# neither plane has one host: a setup template renders with the group layer the hosts share,
# and the compose validator passes each host it renders for.
PLANE_HOSTS = {"k8s": K8S_HOST}


@dataclass(frozen=True)
class Inventory:
    """Where the inventory layers live, so a test can point a reader at a throwaway tree.

    Attributes:
        all_vars: The group_vars file every host shares.
        host_vars: The directory holding one ``<host>.yml`` per inventory host.
        plane_hosts: The host each plane renders with when the caller names none.
        services: The k8s service list, ``cluster/services.yml``.
    """

    all_vars: Path = ALL_VARS
    host_vars: Path = HOST_VARS
    plane_hosts: dict[str, str] = field(default_factory=lambda: dict(PLANE_HOSTS))
    services: Path = K8S_SERVICES


REPO_INVENTORY = Inventory()


def inventory_layers(defaults: dict, group: dict, host: dict) -> dict:
    """The three file layers merged in Ansible's precedence, weakest first.

    Role defaults are Ansible's weakest real layer, so an inventory key of the same name wins,
    and a host's own value wins over the group's.
    """
    return {**defaults, **group, **host}


def role_defaults(role_dir: Path) -> dict:
    """A role's ``defaults/main.yml``, or ``{}`` for a role without one.

    The raw file answers what a role DECLARES: whether it states ``k8s_autodeploy``, which
    ``k8s_claims`` it lists, which ``*_image`` variables it pins. A VALUE the role's tasks use
    can be overridden by the inventory, so read it through ``Estate.role_vars`` instead.
    """
    return load_yaml(role_dir / "defaults" / "main.yml")


class Estate:
    """The inventory one `Inventory` names, each file parsed at most once.

    Args:
        inventory: Where the files live; the repo's own unless a test says not.
    """

    def __init__(self, inventory: Inventory = REPO_INVENTORY) -> None:
        self.inventory = inventory
        self._own: dict[str, dict] = {}

    @cached_property
    def group_vars(self) -> dict:
        """``group_vars/all.yml``, the layer every host shares."""
        return load_yaml(self.inventory.all_vars)

    def hosts(self) -> list[str]:
        """Every host, one per host_vars file, sorted; ``_example.yml`` is not a host."""
        return [p.stem for p in host_files(self.inventory.host_vars)]

    def own_vars(self, host: str) -> dict:
        """What `host`'s host_vars file declares, or ``{}`` for a host without one."""
        if host not in self._own:
            self._own[host] = host_layer(self.inventory.host_vars, host)
        return self._own[host]

    def vars(self, host: str, defaults: dict | None = None) -> dict:
        """`host`'s variables in Ansible's precedence, over `defaults` when a role is in play."""
        return inventory_layers(defaults or {}, self.group_vars, self.own_vars(host))

    def role_vars(self, role_dir: Path, host: str | None = None) -> dict:
        """The values a role's tasks see in a deploy: its defaults under the inventory layers.

        `host` defaults the way `render_context` does: a k8s role takes the k8s plane's host,
        and a setup role, which runs on several hosts, takes the group layer alone. Reading a
        role's defaults raw would print the default even where the inventory overrides it.
        """
        plane = (
            role_dir.parents[1].name
            if role_dir.parent.name == "roles"
            else role_dir.parent.name
        )
        host = host or self.inventory.plane_hosts.get(plane)
        return inventory_layers(
            role_defaults(role_dir),
            self.group_vars,
            self.own_vars(host) if host else {},
        )

    def source(self, host: str, key: str) -> Literal["host", "group"] | None:
        """Which inventory layer `key`'s value for `host` comes from, or None if neither has it.

        A page states a group default as one, rather than as something the host asserted.
        """
        if key in self.own_vars(host):
            return "host"
        if key in self.group_vars:
            return "group"
        return None

    @cached_property
    def services(self) -> list[dict]:
        """Every k8s service's entry, in ``cluster/services.yml`` order."""
        return k8s_services(self.inventory.services)

    def k8s_entries(self) -> dict[str, dict]:
        """Every k8s service's entry, keyed by service name."""
        return {e["name"]: e for e in self.services}
