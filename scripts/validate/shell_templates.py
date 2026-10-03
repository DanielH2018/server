#!/usr/bin/env python3
"""Render every Jinja-templated shell script under ansible/roles/ and lint the output.

Renders with stubbed vars, then lints with `bash -n` + shellcheck. The prek
`bash-syntax-check` / shellcheck hooks gate plain shell files (via identify's
shebang-aware `types = ["shell"]`), but identify tags a `*.sh.j2` template as `{jinja, text}` —
never `shell` — so a Jinja-templated script (e.g. an entrypoint or cron script) is invisible to
both gates no matter how badly it's broken. This is the same render-then-lint pattern as
`validate/compose_templates.py` / `validate/k8s_manifests.py`, extended from YAML parsing to
shell linting: render structure with stubbed vars, then prove the OUTPUT is valid shell.

A render takes the inventory plus the template's own role defaults, laid under it the way
Ansible's precedence does (`template_context`). The defaults are not optional: StubUndefined
iterates empty, so a `{% for %}` over a list a role declares renders its whole body away and
both linters sweep what is left. `ansible/tests/_shell_render.py` renders through this module
for the same reason in reverse — a guard asserting on a rendered script must assert on the
script this gate lints (#3178).

Structural check only: SOPS secrets and other runtime vars are stubbed (StubUndefined, plus a
small override map for values that need to be shell-plausible — see SHELL_STUB_OVERRIDES), so no
SOPS access is needed. Run directly or via the ``validate-shell-templates`` prek hook. Exits
non-zero if any template fails to render, fails `bash -n`, fails shellcheck, or if shellcheck
itself isn't available (a missing linter degrades the gate silently otherwise — fail loud
instead of falling back to bash -n alone).

This module is the entry point. `ansible_jinja_compat` in `scripts/lib/` supplies Ansible's
`search` test and `bool` filter, which vanilla Jinja2 lacks. The pieces only this validator
imports live in `validate_lib/`: `shell_lint` (render, then `bash -n` and shellcheck),
`cron_targets` (which templates a cron `job:` actually schedules) and `cron_checks` (the PATH
and KUBECONFIG rules those targets must satisfy).
"""

import shutil
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from validate.validate_lib.cron_checks import (
    cron_kubeconfig_error,
    cron_path_error,
    cron_uv_interpreter_error,
)
from validate.validate_lib.cron_targets import cron_job_scripts
from lib.k8s_context import resolve_vars
from lib.render_guard import (
    ALL_VARS,
    ANSIBLE,
    BASE_CONTEXT,
    REPO,
    dump_numbered,
    load_yaml,
    render_or_error,
)
from lib.repo_paths import ROLES
from lib.ansible_jinja_env import template_env
from validate.validate_lib.shell_lint import (
    bash_syntax_check,
    find_shellcheck,
    shellcheck_batch,
    shellcheck_check,
)

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


def base_context() -> dict:
    """The inventory layer every shell render starts from: base stubs, all.yml, shell overrides.

    Exposed so `ansible/tests/_shell_render.py` renders a script with the context this gate
    lints it in — one render, two consumers, no second stub set to drift (#3178).
    """
    return {**BASE_CONTEXT, **load_yaml(ALL_VARS), **SHELL_STUB_OVERRIDES}


def template_context(
    path: Path, base: dict | None = None, overrides: dict | None = None
) -> dict:
    """`base` plus the resolved `defaults/main.yml` of the role `path` belongs to.

    A render without a role's own defaults is not the script the host runs. `StubUndefined`
    iterates empty, so `{% for peer in artifacts_peer_sources %}` dropped the whole body of
    `sync-artifacts.sh.j2` — ~45 of its 100 lines, including every rsync call — and both
    linters swept the remainder as if the loop were not there (#3178).

    Role defaults go UNDER the inventory, which is Ansible's own precedence: a group_var or
    host_var beats a role default, so layering them the other way would lint a value no deploy
    produces. `resolve_vars` expands `{{ ... }}` inside a default's own VALUE the way Ansible
    does, so a default that aliases a group_var arrives expanded rather than as literal braces.
    """
    base = base_context() if base is None else base
    role_dir = path.parents[1]  # roles/<plane>/<role>/templates/<name>.sh.j2
    defaults = _resolved_defaults(load_yaml(role_dir / "defaults" / "main.yml"), base)
    return {**defaults, **base, **(overrides or {})}


def _resolved_defaults(raw: dict, base: dict) -> dict:
    """`raw` expanded against `base`, dropping any key whose own expansion raises.

    Two keys in the tree need the drop, each naming a filter `resolve_vars`' light-tier
    environment does not register (its `DECIDED:` marker says why it stays light):
    `gitops_deploy_k8s_autodeploy_denylist` reaches this repo's `k8s_autodeploy_denylist`
    plugin, and `k3s_host_coredns_tarball` reaches ansible-core's `dirname`. No `*.sh.j2`
    reads either key, so taking those roles' eleven shell templates out of the gate over them
    would cost real coverage.

    A dropped key renders as `STUB` through `StubUndefined` rather than as literal braces,
    which is what a guard asserting on a render needs: braces in the output are the defect
    the render exists to remove, so they must never arrive from the context.

    Each drop prints the key and the reason, because a key silently missing from the context
    is how a `{% for %}` over it renders empty and a lint pass sweeps nothing.
    """
    try:
        return resolve_vars(raw, base)
    except Exception as exc:
        print(f"  [note] resolving defaults one key at a time: {exc}", file=sys.stderr)
    resolved: dict = {}
    dropped: set[str] = set()
    # Twice over the file, because a key resolved on its own sees only the keys already done:
    # a default referencing one declared BELOW it would otherwise expand against a missing
    # name and come back carrying `STUB`. The second round re-resolves every surviving key
    # from `raw` with the first round's results in context, so declaration order stops
    # mattering — which is what the whole-dict path's five passes gave for free.
    for _ in range(2):
        for key, value in raw.items():
            if key in dropped:
                continue
            try:
                resolved |= resolve_vars({key: value}, {**base, **resolved})
            except Exception as exc:
                dropped.add(key)
                resolved.pop(key, None)
                print(
                    f"  [note] default {key} left undefined, renders as STUB: {exc}",
                    file=sys.stderr,
                )
    return resolved


def discover_templates() -> list[Path]:
    """Return every *.sh.j2 under ansible/roles/, real templates only.

    Excludes ansible/collections/, the vendored third-party tree, the same way pytest's
    testpaths / ruff's extend-exclude skip it.
    """
    return sorted(ROLES.rglob("*.sh.j2"))


def check_template(
    path: Path,
    ctx: dict,
    out_dir: Path,
    shellcheck_bin: str | None,
    cron_map: dict[Path, Path],
) -> str | None:
    """Render one template, write it under out_dir, and lint the rendered file.

    Writes the rendered file preserving `path`'s relative path (minus the trailing .j2).
    `shellcheck_bin=None` skips the per-file shellcheck step: main() passes None and runs
    `shellcheck_batch` over every rendered file afterwards, which is the same check in one
    process. A caller checking a single template passes the binary and gets it inline.

    Returns:
        An error string, or None on success.
    """
    rel = path.relative_to(ANSIBLE)
    env = template_env(path.parent)
    rendered, err = render_or_error(env, path.name, ctx)
    if rendered is None:
        return err

    out_path = out_dir / rel.with_suffix("")  # drop the trailing .j2
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(rendered)

    err = bash_syntax_check(out_path)
    if err:
        print(f"\n----- rendered {rel} -----", file=sys.stderr)
        dump_numbered(rendered)
        return f"bash -n: {err}"

    if shellcheck_bin is not None:
        err = shellcheck_check(out_path, shellcheck_bin)
        if err:
            print(f"\n----- rendered {rel} -----", file=sys.stderr)
            dump_numbered(rendered)
            return f"shellcheck: {err}"

    err = cron_path_error(path, rendered, cron_map)
    if err:
        return err

    err = cron_kubeconfig_error(path, rendered)
    if err:
        return err

    err = cron_uv_interpreter_error(path, rendered)
    if err:
        return err

    return None


def main(which: Callable[[str], str | None] = shutil.which) -> int:
    """Render every discovered shell template, then `bash -n` and shellcheck the output.

    Args:
        which: the PATH lookup used to find shellcheck. A parameter rather than a module
            attribute so a test can prove the fail-closed branch without patching `shutil`.

    Returns:
        0 if every template rendered clean and passed both linters, 1 otherwise (including
        when shellcheck is missing from PATH or no templates were found).
    """
    shellcheck_bin = find_shellcheck(which)
    if not shellcheck_bin:
        print(
            "[FAIL] shellcheck not found on PATH. It ships via the `shellcheck-py` dev "
            "dependency (pyproject.toml [dependency-groups] dev) — run through `uv run "
            "python scripts/validate/shell_templates.py` (or any `uv run ...`) so uv's synced "
            "venv is on PATH. Failing closed rather than silently degrading to bash -n alone.",
            file=sys.stderr,
        )
        return 1

    templates = discover_templates()
    if not templates:
        print(f"No *.sh.j2 templates found under {ROLES}", file=sys.stderr)
        return 1

    base = base_context()
    cron_map = cron_job_scripts()

    failures = 0
    with tempfile.TemporaryDirectory(prefix="validate-shell-templates-") as tmp:
        out_dir = Path(tmp)
        # Render + `bash -n` + the cron checks per template; shellcheck once over everything
        # that got that far (see shellcheck_batch for why one process, not one per file).
        rendered_ok: dict[Path, Path] = {}
        for path in templates:
            rel = path.relative_to(REPO)
            err = check_template(
                path, template_context(path, base), out_dir, None, cron_map
            )
            if err:
                failures += 1
                print(f"  [FAIL] {rel}: {err}", file=sys.stderr)
            else:
                rendered_ok[out_dir / path.relative_to(ANSIBLE).with_suffix("")] = path
        flagged = shellcheck_batch(list(rendered_ok), shellcheck_bin)
        for out_path, path in rendered_ok.items():
            rel = path.relative_to(REPO)
            if out_path in flagged:
                failures += 1
                print(f"\n----- rendered {rel} -----", file=sys.stderr)
                dump_numbered(out_path.read_text())
                print(
                    f"  [FAIL] {rel}: shellcheck: {flagged[out_path]}", file=sys.stderr
                )
            else:
                print(f"  [ok]   {rel}")

    print(f"\n{len(templates)} shell template(s) checked, {failures} failure(s).")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
