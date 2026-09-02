"""Constants shared by the infra-map inventory, live, model and render stages.

Split out of ``gen_infra_map`` so the stage modules can share them without
importing one another. ``gen_infra_map`` re-exports the public names, so
``REPO_ROOT`` and ``HOSTS`` keep resolving through it.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.repo_paths import REPO as REPO_ROOT  # noqa: F401 -- re-exported


DEFAULT_OUTPUT = Path.home() / ".claude" / "artifacts" / "homelab-infra-map.html"

HOSTS = ("daniel-box", "daniel-server", "daniel-pi")

# What each host actually is. Stated rather than inferred from the platform keys
# in its ``containers_list``: daniel-server's Docker was uninstalled on
# 2026-08-14 and its list emptied, so inference fell through to "docker" and
# every run ssh-ed it for a binary that is gone — the host rendered as an
# unreachable Docker box when it is in fact a healthy k3s agent.
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

# How long the rendered page waits before reloading itself, in seconds. Matches
# the refresh cron's 15-minute period (initial_setup's `infra-map` task) — a
# shorter reload would imply the data is fresher than it is.
PAGE_REFRESH_SECONDS = 900

# Live-collection timeouts. Short on purpose: an unattended run must not hang.
LOCAL_TIMEOUT = 20
SSH_TIMEOUT = 25

_CONTAINER_NAME = re.compile(
    r"^\s*container_name:\s*([A-Za-z0-9_.-]+)\s*$", re.MULTILINE
)

_MANIFEST_KIND = re.compile(r"^kind:\s*([A-Za-z]+)\s*$", re.MULTILINE)

# `claude-otel` is one declared entry that expands to the whole observability
# namespace (collector + loki + prometheus + tempo + grafana). Nothing else in
# the inventory owns a namespace, so an explicit entry beats deriving it by
# rendering the role's Jinja namespace manifest.
NAMESPACE_OWNERS = {"claude-otel": "k8s_observability_namespace"}

_JINJA_VAR = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")
