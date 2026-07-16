#!/usr/bin/env python3
"""Shared helpers for the three render-guard scripts (``validate_compose_templates.py``,
``validate_config_templates.py``, ``validate_shell_templates.py``).

Each renders Jinja templates with stubbed variables and asserts the output is valid (YAML for the
first two, shell for the third). The pieces that are identical across all three — the repo path
anchors, the non-secret fallback context, the plaintext-YAML loader, the numbered-source dumper,
and the base stub-undefined class — live here so they stay in sync instead of being hand-copied.

Imported as ``from _render_guard import ...`` — ``scripts/`` is ``sys.path[0]`` when a validator is
run directly and is on ``sys.path`` under pytest (rootdir insertion, no ``__init__.py``).
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml
from jinja2 import ChainableUndefined

REPO = Path(__file__).resolve().parent.parent
ANSIBLE = REPO / "ansible"
SHARED_TPL = (
    ANSIBLE / "templates"
)  # shared macros (and the labels-macro traefik.yml.j2)
ALL_VARS = ANSIBLE / "inventory" / "group_vars" / "all.yml"

# Non-secret fallbacks for host facts not in the plaintext inventory. Anything still missing
# (SOPS secrets, role vars) renders via StubUndefined — fine for a STRUCTURAL parse/lint check.
BASE_CONTEXT = {
    "docker_network": "proxy",
    "puid": 1000,
    "pgid": 1000,
    "tz": "America/Chicago",
    "sys_user": "ubuntu",
    "email": "stub@example.com",
    "domain": "example.com",
    "server_ip": "10.0.0.1",
    "kuma_docker_host": 1,
}


class StubUndefined(ChainableUndefined):
    """Any undefined variable (a SOPS secret, a host fact) renders as the literal ``STUB`` and
    tolerates attribute/item access and iteration, so structural rendering never aborts on a
    missing value. The config validator subclasses this to add ``__add__``/``__radd__`` for
    ``{{ secret | indent(n) }}``."""

    _FILL = "STUB"

    def __str__(self) -> str:  # {{ secret }}
        return self._FILL

    def __iter__(self):  # {% for x in undefined %}
        return iter(())


def load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


def dump_numbered(text: str) -> None:
    for i, line in enumerate(text.splitlines(), 1):
        print(f"  {i:3d}| {line}", file=sys.stderr)
