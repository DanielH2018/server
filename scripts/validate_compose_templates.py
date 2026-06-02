#!/usr/bin/env python3
"""Render every configured container's docker-compose.yml.j2 and assert the
output parses as valid YAML.

This guards against template edits — especially to the shared ``traefik.yml.j2``
and ``autokuma.yml.j2`` label macros — that silently produce malformed YAML or
broken indentation. It renders structure, not values: secrets and other runtime
variables are stubbed, so no access to the SOPS-encrypted ``secrets.yml`` is
needed.

The container set and per-service parameters are taken from the real
``containers_list`` in each ``inventory/host_vars/*.yml`` file, so each template
is exercised with the same shape it is deployed with (port, hostname, networks,
use_authelia). Commented-out services are skipped automatically (they are not in
the parsed list).

Run directly (``python3 scripts/validate_compose_templates.py``) or via the
``validate-compose-templates`` prek hook. Exits non-zero if any template fails to
render or produces invalid YAML.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml
from jinja2 import ChainableUndefined, Environment, FileSystemLoader

REPO = Path(__file__).resolve().parent.parent
ANSIBLE = REPO / "ansible"
SHARED_TPL = ANSIBLE / "templates"  # traefik.yml.j2 / autokuma.yml.j2 live here
ROLES = ANSIBLE / "roles" / "containers"
HOST_VARS = ANSIBLE / "inventory" / "host_vars"
ALL_VARS = ANSIBLE / "inventory" / "group_vars" / "all.yml"

# Fallback values for variables that are not defined in the (plaintext) inventory
# — e.g. host facts. Anything still missing (SOPS secrets) renders via StubUndefined.
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
    """Any undefined variable (a SOPS secret, a host fact) renders as the literal
    ``STUB`` and tolerates attribute/item access and iteration, so structural
    rendering never aborts on a missing value."""

    _FILL = "STUB"

    def __str__(self) -> str:  # {{ secret }}
        return self._FILL

    def __iter__(self):  # {% for x in undefined %}
        return iter(())


def load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


def build_env(role: str) -> Environment:
    role_tpl_dir = ROLES / role / "templates"
    return Environment(
        loader=FileSystemLoader([str(role_tpl_dir), str(SHARED_TPL)]),
        undefined=StubUndefined,
        # Match Ansible's Templar so rendered whitespace matches a real deploy.
        trim_blocks=True,
        lstrip_blocks=False,
        keep_trailing_newline=True,
    )


def dump_numbered(text: str) -> None:
    for i, line in enumerate(text.splitlines(), 1):
        print(f"  {i:3d}| {line}", file=sys.stderr)


def check_container(host_ctx: dict, ci: dict) -> str | None:
    """Render one container template; return an error string or None on success."""
    name = ci.get("name")
    if not name:
        return None
    tpl = ROLES / name / "templates" / "docker-compose.yml.j2"
    if not tpl.exists():
        return None  # role has no compose template (nothing to validate)

    env = build_env(name)
    ctx = {**host_ctx, "container_item": ci}
    env.globals.update(ctx)
    try:
        rendered = env.get_template("docker-compose.yml.j2").render(**ctx)
    except Exception as exc:  # noqa: BLE001 — surface any render failure
        return f"render error: {type(exc).__name__}: {exc}"

    try:
        list(yaml.safe_load_all(rendered))
    except yaml.YAMLError as exc:
        print(f"\n----- rendered {name}/docker-compose.yml.j2 -----", file=sys.stderr)
        dump_numbered(rendered)
        return f"invalid YAML: {exc}"
    return None


def main() -> int:
    all_vars = load_yaml(ALL_VARS)
    host_files = sorted(HOST_VARS.glob("*.yml"))
    if not host_files:
        print(f"No host_vars found under {HOST_VARS}", file=sys.stderr)
        return 1

    failures = 0
    checked = 0
    for host_file in host_files:
        host_vars = load_yaml(host_file)
        containers = host_vars.get("containers_list") or []
        # host scalars (domain, server_ip, kuma_docker_host, ...) override the base.
        host_ctx = {**BASE_CONTEXT, **all_vars, **host_vars}
        host_ctx.pop("containers_list", None)

        print(f"== {host_file.name} ({len(containers)} active services) ==")
        for ci in containers:
            err = check_container(host_ctx, ci)
            checked += 1
            name = ci.get("name", "<unnamed>")
            if err:
                failures += 1
                print(f"  [FAIL] {name}: {err}", file=sys.stderr)
            else:
                print(f"  [ok]   {name}")

    print(f"\n{checked} template(s) checked, {failures} failure(s).")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
