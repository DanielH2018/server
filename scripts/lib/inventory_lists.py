"""The one reading of the service lists and the host list every script under ``scripts/`` shares.

WHY A MODULE WITH NO IMPORTS. ``render_guard.containers_entries_in`` was meant to be the one
rule for which ``containers_list`` entries count, but importing ``render_guard`` costs a
``jinja2`` and a ``yaml`` import. So eight readers wrote their own filter, and they disagreed
about an entry with an empty ``name`` or none at all (#3357), and host names were copied by
hand into two tuples. This module imports only the standard library and ``repo_paths``, the same reason
``repo_paths`` was split out of ``render_guard``.

``containers_entries_in`` takes an already-parsed mapping, so the caller keeps its own YAML
loader and its own error handling. ``render_guard`` re-exports it.

Three host roles a script must name without parsing YAML: ``LANDING_HOST``, ``PI_HOST``
and ``K8S_HOST`` below. Each is a literal rather than a read of the inventory var that confers the
role, because this module imports no YAML loader. ``scripts/lib/tests/test_inventory_lists.py``
pins each literal to that var instead, so moving the role in the inventory fails a test rather
than leaving every script naming the old host.

A deliberately narrower host set filters ``host_names()`` rather than replacing it.
``fanout_lib/transport.HOSTS`` (the agent hosts) and ``kubectl.CLUSTER_NODES`` (which
``probe.py`` must answer without parsing Ansible, and which names a retired host) stay
where they are.
"""

import sys
from pathlib import Path

# A directly-invoked importer gets only its own directory on sys.path, and pyproject's
# `pythonpath` is a pytest setting.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.repo_paths import HOST_VARS

# The host that runs `land.sh`, the fan-out and the host-package builder.
LANDING_HOST = "daniel-box"
# The one host whose host_vars set `has_docker: true`; its host package ships its compose services.
PI_HOST = "daniel-pi"
# The host whose host_vars every k8s role renders with, the group_vars `k8s_primary_node`. The
# same host as LANDING_HOST, but a different fact: moving the landing host would not move the
# cluster's primary node.
K8S_HOST = "daniel-box"


def host_vars_file(host: str, host_vars: Path = HOST_VARS) -> Path:
    """The host_vars file Ansible reads for `host`, under `host_vars`."""
    return host_vars / f"{host}.yml"


PI_HOST_VARS = host_vars_file(PI_HOST)


def containers_entries_in(data: dict | None) -> list[dict]:
    """The named ``containers_list`` entries in an already-parsed ``cluster/services.yml``.

    Entries with no ``name`` are dropped rather than raising: ``render_guard.entry_tags``
    derives a service's tags from its name, so an unnamed entry selects nothing. A non-mapping entry
    is dropped for the same reason, because it cannot answer ``.get("name")``.
    """
    entries = (data or {}).get("containers_list") or []
    return [e for e in entries if isinstance(e, dict) and e.get("name")]


# The key that lists the Pi's services, which the `homelab-daniel-pi` package deploys (H5).
PI_LIST_KEY = "compose_services"


def pi_entries_in(data: dict | None) -> list[dict]:
    """The named entries of the Pi's `compose_services` in an already-parsed host_vars mapping.

    Entries are filtered as ``containers_entries_in`` filters, and a null list reads as empty.
    A mapping that lacks the key raises, so a reader of the Pi's services cannot read an
    inventory that lost it as a Pi with no services.

    Raises:
        ValueError: `data` lacks `compose_services`.
    """
    data = data or {}
    if PI_LIST_KEY not in data:
        raise ValueError(f"the Pi's host_vars lack {PI_LIST_KEY}")
    return [e for e in data[PI_LIST_KEY] or [] if isinstance(e, dict) and e.get("name")]


def host_names(host_vars: Path = HOST_VARS) -> tuple[str, ...]:
    """Every inventory host, sorted: one per ``host_vars`` file.

    A ``_``-prefixed file, such as ``_example.yml``, is a template for a new host, not a host.
    A missing directory reads as no hosts.
    """
    return tuple(
        sorted(p.stem for p in host_vars.glob("*.yml") if not p.name.startswith("_"))
    )
