#!/usr/bin/env python3
"""The inventory as the docs generators read it: hosts, each host's vars, and its entries.

WHY ONE LOADER. Before #3899 every generator under ``scripts/docs/`` found and parsed the
inventory itself. ``gen_doc_fragments`` read ``all.yml`` and two host_vars files through a
helper named ``role_defaults``, ``fragment_readers.host_has_docker`` and
``reference/hosts._flag`` each re-implemented host-over-group precedence, and
``gen_role_glance``, ``glance_facts``, ``service_catalog`` and ``reference/networking``
each walked ``containers_list`` through a different helper. ``Estate`` is that reading
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

from lib.ansible_inventory import K8S_HOST, PI_HOST, Host, containers_entries_in
from lib.ansible_inventory import inventory_hosts
from lib.render_guard import host_files, load_yaml
from lib.repo_paths import ALL_VARS, HOST_VARS, HOSTS_INI

__all__ = [
    "PLANE_HOSTS",
    "REPO_INVENTORY",
    "Estate",
    "Inventory",
    "inventory_layers",
    "role_defaults",
]

# The host whose host_vars a plane's templates render with when the caller names none. Every
# k8s role deploys from daniel-box's `containers_list`, so its host_vars are the k8s plane's.
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
        hosts_ini: The inventory file naming each host and its groups.
    """

    all_vars: Path = ALL_VARS
    host_vars: Path = HOST_VARS
    plane_hosts: dict[str, str] = field(default_factory=lambda: dict(PLANE_HOSTS))
    hosts_ini: Path = HOSTS_INI


REPO_INVENTORY = Inventory()


def inventory_layers(defaults: dict, group: dict, host: dict) -> dict:
    """The three file layers merged in Ansible's precedence, weakest first.

    Role defaults are Ansible's weakest real layer, so an inventory key of the same name wins,
    and a host's own value wins over the group's.
    """
    return {**defaults, **group, **host}


def role_defaults(role_dir: Path) -> dict:
    """A role's ``defaults/main.yml``, or ``{}`` for a role without one."""
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
    def hosts(self) -> list[Host]:
        """Every host ``hosts.ini`` declares, in file order."""
        return inventory_hosts(self.inventory.hosts_ini)

    @cached_property
    def group_vars(self) -> dict:
        """``group_vars/all.yml``, the layer every host shares."""
        return load_yaml(self.inventory.all_vars)

    def hosts_in(self, group: str) -> list[str]:
        """The hosts ``hosts.ini`` lists under `group`, in file order."""
        return [h.name for h in self.hosts if group in h.groups]

    def host_vars_hosts(self) -> list[str]:
        """Every host with a host_vars file, sorted; ``_example.yml`` is not a host."""
        return [p.stem for p in host_files(self.inventory.host_vars)]

    def own_vars(self, host: str) -> dict:
        """What `host`'s host_vars file declares, or ``{}`` for a host without one."""
        if host not in self._own:
            self._own[host] = load_yaml(self.inventory.host_vars / f"{host}.yml")
        return self._own[host]

    def vars(self, host: str, defaults: dict | None = None) -> dict:
        """`host`'s variables in Ansible's precedence, over `defaults` when a role is in play."""
        return inventory_layers(defaults or {}, self.group_vars, self.own_vars(host))

    def source(self, host: str, key: str) -> Literal["host", "group"] | None:
        """Which inventory layer `key`'s value for `host` comes from, or None if neither has it.

        A page states a group default as one, rather than as something the host asserted.
        """
        if key in self.own_vars(host):
            return "host"
        if key in self.group_vars:
            return "group"
        return None

    def entries(self, host: str) -> list[dict]:
        """`host`'s named ``containers_list`` entries."""
        return containers_entries_in(self.own_vars(host))

    def k8s_entries(self) -> dict[str, dict]:
        """The ``platform: k8s`` entries of the k8s plane's host, keyed by service name."""
        host = self.inventory.plane_hosts.get("k8s", K8S_HOST)
        return {e["name"]: e for e in self.entries(host) if e.get("platform") == "k8s"}

    def pi_entries(self, host: str = PI_HOST) -> list[dict]:
        """The Docker host's Docker entries; the platform default there is Docker."""
        return [e for e in self.entries(host) if e.get("platform", "docker") != "k8s"]
