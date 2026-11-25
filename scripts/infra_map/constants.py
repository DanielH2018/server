"""Constants shared by the infra-map inventory, live, model and render stages.

Split out of ``gen_infra_map`` so the stage modules can share them without
importing one another. ``gen_infra_map`` re-exports the public names, so
``REPO_ROOT`` and ``HOSTS`` keep resolving through it.
"""

import re
from pathlib import Path

from lib.inventory_lists import host_names
from lib.repo_paths import REPO as REPO_ROOT  # noqa: F401 -- re-exported


DEFAULT_OUTPUT = Path.home() / ".claude" / "artifacts" / "homelab-infra-map.html"

HOSTS = host_names()

# Stated, not inferred: daniel-server lists no services, so inference would fall to 'docker' and
# render a healthy k3s agent as unreachable.
HOST_PLANE = {
    "daniel-box": "k8s",
    "daniel-server": "k8s",
    "daniel-pi": "docker",
}

# The sub-role within the plane, for the host panel and the diagram's node boxes.
HOST_ROLE = {
    "daniel-box": "k3s server · control plane",
    "daniel-server": "k3s agent",
    "daniel-pi": "Docker · LAN-only",
}

# Matches the 15-minute refresh period: a shorter reload would imply fresher data.
PAGE_REFRESH_SECONDS = 900

# Live-collection timeouts. Short on purpose: an unattended run must not hang.
LOCAL_TIMEOUT = 20
SSH_TIMEOUT = 25

_CONTAINER_NAME = re.compile(
    r"^\s*container_name:\s*([A-Za-z0-9_.-]+)\s*$", re.MULTILINE
)

_MANIFEST_KIND = re.compile(r"^kind:\s*([A-Za-z]+)\s*$", re.MULTILINE)

# One declared entry expands to the whole observability namespace; nothing else in the inventory
# owns a namespace.
NAMESPACE_OWNERS = {"observability": "k8s_observability_namespace"}

_JINJA_VAR = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")
