#!/usr/bin/env python3
"""Shared helpers for the render-guard scripts and other Ansible-inventory readers.

Used by ``validate_compose_templates.py``, ``validate_config_templates.py``, and
``validate_shell_templates.py``, plus the other scripts that read the same Ansible
inventory.

Each render guard renders Jinja templates with stubbed variables and asserts the output is valid
(YAML for the first two, shell for the third). The pieces that are identical across all three —
the repo path anchors, the non-secret fallback context, the plaintext-YAML loader, the numbered-
source dumper, and the base stub-undefined class — live here so they stay in sync instead of
being hand-copied.

The inventory readers at the bottom (``host_files``, ``containers_entries``) serve a wider set:
``deploy_tags.py`` and ``service_catalog.py`` both walked ``host_vars/`` and parsed
``containers_list`` themselves, and each carried its own copy of the ``_``-prefix exclusion.
The path anchors come from ``repo_paths.py`` and are re-exported here for the callers that
already import them from this module; a script that wants only a path imports
``repo_paths`` directly and skips the ``jinja2`` import this module costs.

Imported as ``from lib.render_guard import ...`` after the caller's own ``sys.path``
bootstrap puts ``scripts/`` on the path (repo-root CLAUDE.md, *Directory Structure*).
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml
from jinja2 import ChainableUndefined, Environment, FileSystemLoader

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.repo_paths import (
    ALL_VARS,
    ANSIBLE,
    HOST_VARS,
    INVENTORY,
    REPO,
    SHARED_TPL,
)

__all__ = [
    "ALL_VARS",
    "ANSIBLE",
    "BASE_CONTEXT",
    "HOST_VARS",
    "INVENTORY",
    "REPO",
    "SHARED_TPL",
    "StubUndefined",
    "containers_entries",
    "dump_numbered",
    "host_files",
    "load_yaml",
    "make_env",
    "render_or_error",
]

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
    """A Jinja undefined value that renders as the literal ``STUB`` instead of raising.

    Used for any undefined variable — a SOPS secret, a host fact — so structural
    rendering never aborts on a missing value. Tolerates attribute/item access,
    iteration, and concatenation (Jinja's ``indent`` filter prepends a newline via
    ``+``, so ``{{ secret | indent(n) }}`` needs ``__add__``).
    """

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
    """A YAML mapping, or ``{}`` for a missing, empty or non-mapping file.

    Every caller indexes the result as a dict, so a file whose top level is a list or a
    scalar returns ``{}`` rather than leaking through to a ``TypeError`` several calls later.
    """
    if not path.is_file():
        return {}
    loaded = yaml.safe_load(path.read_text())
    return loaded if isinstance(loaded, dict) else {}


def dump_numbered(text: str) -> None:
    for i, line in enumerate(text.splitlines(), 1):
        print(f"  {i:3d}| {line}", file=sys.stderr)


def make_env(dirs, undefined_cls=StubUndefined) -> Environment:
    """The Jinja Environment shared by the three render guards.

    Carries the given template dirs on the loader, the stub-undefined class, and the whitespace
    flags matching Ansible's Templar so rendered output matches a real deploy. Callers register any
    Ansible filter/test shims (the compose guard's ``hash`` filter, the shell guard's ``search``
    test) on the returned env.
    """
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
    identical error format all three guards wrapped their render call in.
    """
    env.globals.update(ctx)
    try:
        return env.get_template(name).render(**ctx), None
    except Exception as exc:
        return None, f"render error: {type(exc).__name__}: {exc}"


def host_files(host_vars: Path = HOST_VARS) -> list[Path]:
    """Every real host's ``host_vars`` file, sorted.

    ``_example.yml`` is a template for a new host, not a host — Ansible only loads a host_vars
    file whose name matches an inventory hostname, so it is inert to a real run and must be
    inert here too. ``ansible/tests/deploy/test_containers_list_roles_exist.py`` makes the same
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
