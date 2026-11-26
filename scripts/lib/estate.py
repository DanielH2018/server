#!/usr/bin/env python3
"""The inventory as the docs generators read it: hosts, each host's vars, and its entries.

WHY ONE LOADER. Every generator under ``scripts/docs/`` would otherwise find and parse the
inventory itself: read ``all.yml`` and host_vars files through its own helper, re-implement
host-over-group precedence, and walk the service list through its own helper. ``Estate`` is
that reading done once; a generator asks it for values and only renders.

THE PRECEDENCE RULE LIVES HERE, ONCE. ``inventory_layers`` is the order Ansible applies,
role defaults < the shared vars files < a host's own file. ``render_context`` builds its render
context on top of it, adding the stubs and the Jinja resolution a template render needs.
The docs generators take the layers RAW: a page prints what the inventory says, and an
expanded or ``STUB``-ed value would be a value no file holds.

WHERE IT READS. An ``Inventory`` names the files, defaulting to the repo's own. A test
points one at a ``tmp_path`` tree; nothing here reads a module constant past it.

Typical usage example:

    estate = Estate()
    estate.vars("daniel-pi")["has_fwupd"]  # False
    estate.source("daniel-pi", "has_fwupd")  # "group": only hosts/vars.yml declares it
"""

from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Literal

from lib.container_entry import ContainerEntry
from lib.inventory_lists import host_names
from lib.render_guard import host_layer, k8s_services, load_yaml
from lib.repo_paths import ESTATE_VARS, HOST_VARS, K8S_SERVICES, K8S_VARS

__all__ = [
    "REPO_INVENTORY",
    "Estate",
    "Inventory",
    "host_vars",
    "inventory_layers",
    "role_defaults",
    "shared_vars",
]


@dataclass(frozen=True)
class Inventory:
    """Where the inventory layers live, so a test can point a reader at a throwaway tree.

    Attributes:
        estate_vars: ``estate/vars.yml``, the facts every render reads.
        k8s_vars: ``k8s/vars.yml``, the cluster's facts, which every render reads too.
        host_vars: The hosts directory: ``vars.yml`` for every host, ``<host>/vars.yml`` for one.
        services: The k8s service list, ``k8s/services.yml``.
    """

    estate_vars: Path = ESTATE_VARS
    k8s_vars: Path = K8S_VARS
    host_vars: Path = HOST_VARS
    services: Path = K8S_SERVICES

    def shared_files(self, cluster: bool = False) -> tuple[Path, ...]:
        """The shared vars files, weakest first; a `cluster` render reads no host defaults."""
        files = (self.estate_vars, self.k8s_vars)
        return files if cluster else (*files, self.host_vars / "vars.yml")

    def shared_layer(self, cluster: bool = False) -> dict:
        """The shared files merged; see `shared_files`.

        Raises:
            ValueError: two shared files declare the same key, so which one a render reads
              would depend on the layer order rather than on where the key belongs.
        """
        merged: dict = {}
        owner: dict[str, Path] = {}
        for path in self.shared_files(cluster):
            layer = load_yaml(path)
            for key in layer.keys() & owner.keys():
                raise ValueError(f"{key} is declared in both {owner[key]} and {path}")
            owner |= dict.fromkeys(layer, path)
            merged |= layer
        return merged

    def var_files(self) -> list[Path]:
        """Every vars file, sorted: the shared ones and each host's, `_example`'s included."""
        return sorted([*self.shared_files(), *self.host_vars.glob("*/vars.yml")])


REPO_INVENTORY = Inventory()


def shared_vars() -> dict:
    """The repo's shared layer, raw: estate, k8s and every-host vars merged."""
    return REPO_INVENTORY.shared_layer()


def host_vars(host: str) -> dict:
    """What `host`'s own file in the repo declares, raw, or ``{}`` for a host without one."""
    return host_layer(REPO_INVENTORY.host_vars, host)


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
        """The shared layer every host reads: estate, k8s and every-host vars merged."""
        return self.inventory.shared_layer()

    def hosts(self) -> list[str]:
        """Every host, one per ``<host>/vars.yml``, sorted; ``_example`` is not a host."""
        return list(host_names(self.inventory.host_vars))

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

        With no `host`, the group layer alone overrides the defaults, as in `render_context`.
        Reading a role's defaults raw would print the default even where the inventory
        overrides it.
        """
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
    def services(self) -> tuple[ContainerEntry, ...]:
        """Every k8s service's entry, in ``k8s/services.yml`` order."""
        return k8s_services(self.inventory.services)

    def k8s_entries(self) -> dict[str, dict]:
        """Every k8s service's entry, keyed by service name."""
        return {e["name"]: e for e in self.services}
