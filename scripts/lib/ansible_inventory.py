"""The one reading of ``containers_list`` and ``hosts.ini`` every script under ``scripts/`` shares.

WHY A MODULE WITH NO IMPORTS. ``render_guard.containers_entries_in`` was meant to be the one
rule for which ``containers_list`` entries count, but importing ``render_guard`` costs a
``jinja2`` and a ``yaml`` import. So eight readers wrote their own filter, and they disagreed
about an entry with an empty ``name`` or none at all (#3357). ``hosts.ini`` was parsed twice
with different rules for an inline comment, and its host names were copied by hand into two
tuples. This module imports only ``pathlib`` and ``repo_paths``, the same reason
``repo_paths`` was split out of ``render_guard``.

``containers_entries_in`` takes an already-parsed mapping, so the caller keeps its own YAML
loader and its own error handling. ``render_guard`` re-exports it.

Two hosts hold a role a script must name without parsing YAML: ``GITOPS_HOST`` and
``PI_HOST`` below. Each is a literal rather than a read of the inventory var that confers the
role, because this module imports no YAML loader. ``scripts/lib/tests/test_ansible_inventory.py``
pins each literal to that var instead, so moving the role in the inventory fails a test rather
than leaving every script naming the old host.

A deliberately narrower host set filters ``inventory_hosts()`` rather than replacing it.
``fanout_lib/transport.HOSTS`` (the agent hosts) and ``kubectl.CLUSTER_NODES`` (which
``probe.py`` must answer without parsing Ansible, and which names a retired host) stay
where they are.
"""

import sys
from dataclasses import dataclass
from pathlib import Path

# A directly-invoked importer gets only its own directory on sys.path, and pyproject's
# `pythonpath` is a pytest setting.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.repo_paths import HOSTS_INI

# The one host whose host_vars set `has_gitops: true`: it runs the GitOps deployer and
# `land.sh`. group_vars defaults the key to false, so a host is armed only by saying so.
GITOPS_HOST = "daniel-box"
# The one host whose host_vars set `has_docker: true`, driven over ssh with `-e target=`.
PI_HOST = "daniel-pi"


def containers_entries_in(data: dict | None) -> list[dict]:
    """The named ``containers_list`` entries in an already-parsed host_vars mapping.

    Entries with no ``name`` are dropped rather than raising: ``deploy.yml`` derives a
    service's tags from its name, so an unnamed entry selects nothing. A non-mapping entry
    is dropped for the same reason, because it cannot answer ``.get("name")``.
    """
    entries = (data or {}).get("containers_list") or []
    return [e for e in entries if isinstance(e, dict) and e.get("name")]


@dataclass(frozen=True)
class Host:
    """One host line of ``hosts.ini``.

    Attributes:
        name: the inventory hostname.
        groups: every ``[group]`` section the host line sits under, in file order.
        settings: the line's ``key=value`` host variables, such as ``ansible_host``.
    """

    name: str
    groups: tuple[str, ...]
    settings: dict[str, str]

    @property
    def connection(self) -> str:
        """``ansible_connection``, defaulting to ``ssh`` as Ansible does."""
        return self.settings.get("ansible_connection", "ssh")


def inventory_hosts(path: Path = HOSTS_INI) -> list[Host]:
    """Every host ``hosts.ini`` declares, in file order, one entry per name.

    Parsed by hand rather than with ``configparser``: an ini host line is
    ``name key=value key=value``, which ``configparser`` reads as one long key. A ``#``
    anywhere starts a comment, and so does a ``;`` at the start of a line, as Ansible's ini
    parser reads them. A ``[group:vars]`` or ``[group:children]`` section holds no host
    lines, so it is skipped. A host listed under two groups is one ``Host`` carrying both.
    A missing file reads as no hosts.
    """
    if not path.is_file():
        return []
    order: list[str] = []
    groups: dict[str, list[str]] = {}
    settings: dict[str, dict[str, str]] = {}
    section: str | None = None
    for raw in path.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith(";"):
            continue
        if line.startswith("["):
            section = line.strip("[]").strip()
            continue
        if section is not None and ":" in section:
            continue
        name, *pairs = line.split()
        if name not in settings:
            order.append(name)
            groups[name] = []
            settings[name] = {}
        if section is not None and section not in groups[name]:
            groups[name].append(section)
        for pair in pairs:
            if "=" in pair:
                key, value = pair.split("=", 1)
                settings[name][key] = value
    return [Host(n, tuple(groups[n]), settings[n]) for n in order]


def host_names(path: Path = HOSTS_INI) -> tuple[str, ...]:
    """Every inventory hostname, in file order."""
    return tuple(h.name for h in inventory_hosts(path))
