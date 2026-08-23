#!/usr/bin/env python3
"""Shared helpers for the render-guard scripts (``validate_compose_templates.py``,
``validate_config_templates.py``, ``validate_shell_templates.py``) and for the other scripts
that read the same Ansible inventory.

Each render guard renders Jinja templates with stubbed variables and asserts the output is valid
(YAML for the first two, shell for the third). The pieces that are identical across all three —
the repo path anchors, the non-secret fallback context, the plaintext-YAML loader, the numbered-
source dumper, and the base stub-undefined class — live here so they stay in sync instead of
being hand-copied.

The inventory readers at the bottom (``host_files``, ``containers_entries``) serve a wider set:
``deploy_tags.py`` and ``service_catalog.py`` both walked ``host_vars/`` and parsed
``containers_list`` themselves, and each carried its own copy of the ``_``-prefix exclusion.
Six scripts anchored their own path to ``inventory/`` before this; the anchors below are the
one definition.

Imported as ``from _render_guard import ...`` — ``scripts/`` is ``sys.path[0]`` when a validator is
run directly and is on ``sys.path`` under pytest (rootdir insertion, no ``__init__.py``).
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml
from jinja2 import ChainableUndefined, Environment, FileSystemLoader

REPO = Path(__file__).resolve().parent.parent
ANSIBLE = REPO / "ansible"
SHARED_TPL = (
    ANSIBLE / "templates"
)  # shared macros (and the labels-macro traefik.yml.j2)
INVENTORY = ANSIBLE / "inventory"
ALL_VARS = INVENTORY / "group_vars" / "all.yml"
HOST_VARS = INVENTORY / "host_vars"

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
    tolerates attribute/item access, iteration, and concatenation (Jinja's ``indent`` filter
    prepends a newline via ``+``, so ``{{ secret | indent(n) }}`` needs ``__add__``), so
    structural rendering never aborts on a missing value."""

    _FILL = "STUB"

    def __str__(self) -> str:  # {{ secret }}
        return self._FILL

    def __iter__(self):  # {% for x in undefined %}
        return iter(())

    def __add__(self, other):  # {{ secret | indent(n) }}
        return self._FILL + str(other)

    def __radd__(self, other):
        return str(other) + self._FILL


def load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


def dump_numbered(text: str) -> None:
    for i, line in enumerate(text.splitlines(), 1):
        print(f"  {i:3d}| {line}", file=sys.stderr)


def make_env(dirs, undefined_cls=StubUndefined) -> Environment:
    """The Jinja Environment shared by the three render guards: the given template dirs on the
    loader, the stub-undefined class, and the whitespace flags matching Ansible's Templar so
    rendered output matches a real deploy. Callers register any Ansible filter/test shims
    (the compose guard's ``hash`` filter, the shell guard's ``search`` test) on the returned env."""
    return Environment(
        loader=FileSystemLoader([str(d) for d in dirs]),
        undefined=undefined_cls,
        trim_blocks=True,
        lstrip_blocks=False,
        keep_trailing_newline=True,
    )


def render_or_error(
    env: Environment, name: str, ctx: dict
) -> tuple[str | None, str | None]:
    """Render template ``name`` with ``ctx`` (also injected as globals so imported macros see it).
    Return ``(rendered, None)`` on success or ``(None, "render error: <Type>: <msg>")`` — the
    identical error format all three guards wrapped their render call in."""
    env.globals.update(ctx)
    try:
        return env.get_template(name).render(**ctx), None
    except Exception as exc:  # noqa: BLE001 — surface any render failure
        return None, f"render error: {type(exc).__name__}: {exc}"


def host_files(host_vars: Path = HOST_VARS) -> list[Path]:
    """Every real host's ``host_vars`` file, sorted.

    ``_example.yml`` is a template for a new host, not a host — Ansible only loads a host_vars
    file whose name matches an inventory hostname, so it is inert to a real run and must be
    inert here too. ``ansible/tests/test_containers_list_roles_exist.py`` makes the same
    exclusion, and ``test_deploy_tags.py::test_example_host_vars_is_not_a_source_of_tags`` pins
    it. The ``host_vars`` argument stays a parameter because every caller's tests inject a
    ``tmp_path`` through it.
    """
    return sorted(p for p in host_vars.glob("*.yml") if not p.name.startswith("_"))


def containers_entries(path: Path) -> list[dict]:
    """The named ``containers_list`` entries in one host_vars file.

    Entries with no ``name`` are dropped rather than raising: every caller skipped them
    individually before, because ``deploy.yml`` derives a service's tags from its name and an
    unnamed entry selects nothing. A non-mapping entry is dropped for the same reason — it
    cannot answer ``.get("name")``.
    """
    entries = load_yaml(path).get("containers_list") or []
    return [e for e in entries if isinstance(e, dict) and e.get("name")]
