#!/usr/bin/env python3
"""Render every Jinja-templated shell script under ansible/roles/ with stubbed vars and lint
the output (`bash -n` + shellcheck).

The prek `bash-syntax-check` / shellcheck hooks gate plain shell files (via identify's
shebang-aware `types = ["shell"]`), but identify tags a `*.sh.j2` template as `{jinja, text}` —
never `shell` — so a Jinja-templated script (e.g. an entrypoint or cron script) is invisible to
both gates no matter how badly it's broken. This is the same render-then-lint pattern as
`validate_compose_templates.py` / `validate_config_templates.py`, extended from YAML parsing to
shell linting: render structure with stubbed vars, then prove the OUTPUT is valid shell.

Structural check only: SOPS secrets and other runtime vars are stubbed (StubUndefined, plus a
small override map for values that need to be shell-plausible — see SHELL_STUB_OVERRIDES), so no
SOPS access is needed. Run directly or via the ``validate-shell-templates`` prek hook. Exits
non-zero if any template fails to render, fails `bash -n`, fails shellcheck, or if shellcheck
itself isn't available (a missing linter degrades the gate silently otherwise — fail loud
instead of falling back to bash -n alone).
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml
from jinja2 import ChainableUndefined, Environment, FileSystemLoader


def _ansible_search(value, pattern, ignorecase=False, multiline=False) -> bool:
    """Mirror Ansible's `search` Jinja test (ansible.plugins.test.core) — a plain regex search,
    not a full match. Vanilla Jinja2 has no `search` test, so docker-user-rules.sh.j2's
    `cloudflare_ips | reject('search', ':')` (splitting the IPv4/IPv6 Cloudflare ranges) would
    otherwise fail to render here with `TemplateRuntimeError: No test named 'search'`."""
    flags = (re.I if ignorecase else 0) | (re.M if multiline else 0)
    return bool(re.search(pattern, str(value), flags))


REPO = Path(__file__).resolve().parent.parent
ANSIBLE = REPO / "ansible"
SHARED_TPL = ANSIBLE / "templates"
ROLES = ANSIBLE / "roles"
ALL_VARS = ANSIBLE / "inventory" / "group_vars" / "all.yml"

# Fallback values for variables that are not defined in the (plaintext) inventory — e.g. SOPS
# secrets. Anything still missing renders via StubUndefined. Mirrors validate_compose_templates.py
# / validate_config_templates.py so the three render guards stay consistent.
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

# Shell-specific overrides: values a lint pass needs to be shell-plausible rather than the bare
# "STUB" literal StubUndefined fills in elsewhere. A bare "STUB" would actually be fine for a
# plain string interpolation, but these three are structurally different:
#  - the two push tokens are SOPS secrets with no plaintext fallback in group_vars/all.yml (unlike
#    cloudflare_ips/sys_user below, which ARE plaintext and come through BASE_CONTEXT/all_vars
#    unchanged) — any token-shaped string is fine, they're just interpolated into a URL path.
#  - `hostvars` is Ansible's own magic var (host facts keyed by inventory hostname), not something
#    vanilla Jinja provides. pi-sd-health.sh.j2 only dereferences hostvars['daniel-server'].server_ip,
#    so stub just that path — Jinja's `.attr` lookup falls back to `dict.__getitem__` when the
#    attribute doesn't exist, so a plain nested dict renders identically to the real Ansible object.
SHELL_STUB_OVERRIDES = {
    "secret_rotation_push_token": "stub-secret-rotation-token",
    "pi_sd_health_push_token": "stub-pi-sd-health-token",
    "hostvars": {"daniel-server": {"server_ip": "10.0.0.1"}},
}


class StubUndefined(ChainableUndefined):
    """Any undefined variable (a SOPS secret, a host fact) renders as the literal ``STUB`` and
    tolerates attribute/item access and iteration, so structural rendering never aborts on a
    missing value. Backstop only — every var actually referenced by the current templates is
    covered by BASE_CONTEXT / all.yml / SHELL_STUB_OVERRIDES above, precisely so a lone "STUB"
    never has to survive being embedded in a shell comparison/arithmetic/format context."""

    _FILL = "STUB"

    def __str__(self) -> str:
        return self._FILL

    def __iter__(self):
        return iter(())


def load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


def discover_templates() -> list[Path]:
    """Every *.sh.j2 under ansible/roles/ (real templates only — ansible/collections/ is the
    vendored third-party tree and is excluded the same way pytest's testpaths / ruff's
    extend-exclude skip it)."""
    return sorted(ROLES.rglob("*.sh.j2"))


def build_env(template_dir: Path) -> Environment:
    env = Environment(
        loader=FileSystemLoader([str(template_dir), str(SHARED_TPL)]),
        undefined=StubUndefined,
        # Match Ansible's Templar so rendered whitespace matches a real deploy.
        trim_blocks=True,
        lstrip_blocks=False,
        keep_trailing_newline=True,
    )
    env.tests["search"] = (
        _ansible_search  # used by docker-user-rules.sh.j2's IPv4/IPv6 split
    )
    return env


def render_template(path: Path, ctx: dict) -> str:
    env = build_env(path.parent)
    env.globals.update(ctx)
    return env.get_template(path.name).render(**ctx)


def dump_numbered(text: str) -> None:
    for i, line in enumerate(text.splitlines(), 1):
        print(f"  {i:3d}| {line}", file=sys.stderr)


def bash_syntax_check(path: Path) -> str | None:
    """`bash -n` parses (never executes) the rendered script. Return an error string, or None."""
    proc = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
    if proc.returncode != 0:
        return proc.stderr.strip() or f"bash -n exited {proc.returncode}"
    return None


def shellcheck_check(path: Path, shellcheck_bin: str) -> str | None:
    """Run shellcheck (all severities — the repo default, no --severity override, matching the
    prek shellcheck hook) against the rendered script. Return an error string, or None."""
    proc = subprocess.run([shellcheck_bin, str(path)], capture_output=True, text=True)
    if proc.returncode != 0:
        return (
            proc.stdout.strip()
            or proc.stderr.strip()
            or f"shellcheck exited {proc.returncode}"
        )
    return None


def check_template(
    path: Path, ctx: dict, out_dir: Path, shellcheck_bin: str
) -> str | None:
    """Render one template, write it under out_dir preserving its relative path (minus the
    trailing .j2), then lint the rendered file. Return an error string, or None on success."""
    rel = path.relative_to(ANSIBLE)
    try:
        rendered = render_template(path, ctx)
    except Exception as exc:  # noqa: BLE001 — surface any render failure
        return f"render error: {type(exc).__name__}: {exc}"

    out_path = out_dir / rel.with_suffix("")  # drop the trailing .j2
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(rendered)

    err = bash_syntax_check(out_path)
    if err:
        print(f"\n----- rendered {rel} -----", file=sys.stderr)
        dump_numbered(rendered)
        return f"bash -n: {err}"

    err = shellcheck_check(out_path, shellcheck_bin)
    if err:
        print(f"\n----- rendered {rel} -----", file=sys.stderr)
        dump_numbered(rendered)
        return f"shellcheck: {err}"

    return None


def main() -> int:
    shellcheck_bin = shutil.which("shellcheck")
    if not shellcheck_bin:
        print(
            "[FAIL] shellcheck not found on PATH. It ships via the `shellcheck-py` dev "
            "dependency (pyproject.toml [dependency-groups] dev) — run through `uv run "
            "python scripts/validate_shell_templates.py` (or any `uv run ...`) so uv's synced "
            "venv is on PATH. Failing closed rather than silently degrading to bash -n alone.",
            file=sys.stderr,
        )
        return 1

    templates = discover_templates()
    if not templates:
        print(f"No *.sh.j2 templates found under {ROLES}", file=sys.stderr)
        return 1

    all_vars = load_yaml(ALL_VARS)
    ctx = {**BASE_CONTEXT, **all_vars, **SHELL_STUB_OVERRIDES}

    failures = 0
    with tempfile.TemporaryDirectory(prefix="validate-shell-templates-") as tmp:
        out_dir = Path(tmp)
        for path in templates:
            rel = path.relative_to(REPO)
            err = check_template(path, ctx, out_dir, shellcheck_bin)
            if err:
                failures += 1
                print(f"  [FAIL] {rel}: {err}", file=sys.stderr)
            else:
                print(f"  [ok]   {rel}")

    print(f"\n{len(templates)} shell template(s) checked, {failures} failure(s).")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
