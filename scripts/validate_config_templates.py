#!/usr/bin/env python3
"""Render the high-value NON-compose YAML config templates (auth / proxy / monitoring) with
stubbed vars and assert each parses as valid YAML.

The container *compose* templates already have this guard (validate_compose_templates.py), but
the bind-mounted *config* templates did not — yet they re-render on every deploy of an
auth/proxy/monitoring-critical service. A Jinja indentation bug here is exactly the class
``check-yaml`` and ``ansible-lint`` miss (they don't render ``.j2``), so it would pass CI and
only surface at deploy. Worse, a config-only push is health-gated but auth-critical, so that's a
bad place to first discover a broken authelia/traefik config.

Structural check only: secrets and host vars are stubbed (StubUndefined), so no SOPS access is
needed. Run directly or via the ``validate-config-templates`` prek hook. Exits non-zero on any
render failure or invalid YAML.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml
from jinja2 import ChainableUndefined, Environment, FileSystemLoader

REPO = Path(__file__).resolve().parent.parent
ANSIBLE = REPO / "ansible"
SHARED_TPL = ANSIBLE / "templates"  # shared macros (and the labels-macro traefik.yml.j2)
ROLES = ANSIBLE / "roles" / "containers"
ALL_VARS = ANSIBLE / "inventory" / "group_vars" / "all.yml"

# Role-relative config templates to validate. NOT docker-compose.yml.j2 (that's the compose
# validator's job). These are bind-mounted, re-rendered every deploy, Jinja-bearing, and gate
# auth / reverse-proxy / monitoring. The role's own templates dir takes loader precedence, so
# `traefik/traefik.yml.j2` resolves to Traefik's STATIC config, not the shared labels macro.
CONFIG_TEMPLATES = [
    "authelia/configuration.yml.j2",
    "traefik/config.yml.j2",
    "traefik/traefik.yml.j2",
    "prometheus/prometheus.yml.j2",
    "grafana/loki-config.yml.j2",
    "grafana/promtail-config.yml.j2",
]

# Non-secret fallbacks for host facts not in the plaintext inventory. Anything still missing
# (SOPS secrets, role vars) renders via StubUndefined — fine for a STRUCTURAL parse check.
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
    """Any undefined variable renders as the literal ``STUB`` and tolerates str/concat/iter, so a
    structural render never aborts on a missing secret/host var — including ``{{ secret | indent(n) }}``
    (Jinja's ``indent`` does ``s += "\\n"``, which a bare Undefined can't, hence ``__add__``)."""

    _FILL = "STUB"

    def __str__(self) -> str:
        return self._FILL

    def __iter__(self):
        return iter(())

    def __add__(self, other):
        return self._FILL + str(other)

    def __radd__(self, other):
        return str(other) + self._FILL


def load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


def build_env(role: str) -> Environment:
    return Environment(
        loader=FileSystemLoader([str(ROLES / role / "templates"), str(SHARED_TPL)]),
        undefined=StubUndefined,
        # Match Ansible's Templar so rendered whitespace matches a real deploy.
        trim_blocks=True,
        lstrip_blocks=False,
        keep_trailing_newline=True,
    )


def yaml_error(rendered: str) -> str | None:
    """Return an error string if ``rendered`` is not parseable YAML, else None."""
    try:
        list(yaml.safe_load_all(rendered))
    except yaml.YAMLError as exc:
        return f"invalid YAML: {exc}"
    return None


def check_template(rel: str, ctx: dict) -> str | None:
    """Render one role-relative config template; return an error string or None on success."""
    role, name = rel.split("/", 1)
    tpl = ROLES / role / "templates" / name
    if not tpl.exists():
        return f"missing template {tpl}"

    env = build_env(role)
    env.globals.update(ctx)
    try:
        rendered = env.get_template(name).render(**ctx)
    except Exception as exc:  # noqa: BLE001 — surface any render failure
        return f"render error: {type(exc).__name__}: {exc}"

    err = yaml_error(rendered)
    if err:
        print(f"\n----- rendered {rel} -----", file=sys.stderr)
        for i, line in enumerate(rendered.splitlines(), 1):
            print(f"  {i:3d}| {line}", file=sys.stderr)
    return err


def main() -> int:
    ctx = {**BASE_CONTEXT, **load_yaml(ALL_VARS)}
    failures = 0
    for rel in CONFIG_TEMPLATES:
        err = check_template(rel, ctx)
        if err:
            failures += 1
            print(f"  [FAIL] {rel}: {err}", file=sys.stderr)
        else:
            print(f"  [ok]   {rel}")
    print(f"\n{len(CONFIG_TEMPLATES)} config template(s) checked, {failures} failure(s).")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
