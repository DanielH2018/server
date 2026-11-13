#!/usr/bin/env python3
"""Render every Jinja-templated shell script under ansible/roles/ and hosts/components/, then lint.

Renders with stubbed vars, then lints with `bash -n` + shellcheck. The prek
`bash-syntax-check` / shellcheck hooks gate plain shell files (via identify's
shebang-aware `types = ["shell"]`), but identify tags a `*.sh.j2` template as `{jinja, text}` —
never `shell` — so a Jinja-templated script (e.g. an entrypoint or cron script) is invisible to
both gates no matter how badly it's broken. This is the same render-then-lint pattern as
`validate/compose_templates.py` / `validate/k8s_manifests.py`, extended from YAML parsing to
shell linting: render structure with stubbed vars, then prove the OUTPUT is valid shell.

A render takes the inventory plus the template's own role defaults, laid under it the way
Ansible's precedence does (`lib.render_context`). A host component's template takes its
`component.yml` `defaults:` in the same place, and the host whose package lists the component,
as the host renderer does. The defaults are not optional: StubUndefined
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

This module is the entry point. It renders each template through
`validate_lib/shipped_render.py`, the renderer's own environment, so the linters read the
bytes the package ships. The lint pieces only this validator imports live in
`validate_lib/shell_lint.py` (`bash -n` and shellcheck).
"""

import shutil
import sys
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from validate.validate_lib.ok_lines import ok_printer
from lib.cli_help import answer_help
from lib.estate import role_defaults
from lib.render_context import render_context
from lib.render_guard import (
    REPO,
    dump_numbered,
)
from lib import yaml_fast
from lib.repo_paths import ROLES
from validate.validate_lib.shipped_render import render_shipped
from validate.validate_lib.shell_lint import (
    bash_syntax_check,
    find_shellcheck,
    shellcheck_batch,
    shellcheck_check,
)

# The host components whose templates this gate also lints (H3.8b).
COMPONENTS = REPO / "hosts" / "components"

# Shell-specific overrides: values a lint pass needs to be shell-plausible rather than the bare
# "STUB" literal StubUndefined fills in elsewhere. No linted template needs one. `hostvars` needs
# no stub: `render_context` builds it from each host's literal `server_ip`, and a stub here would
# replace that whole map.
SHELL_STUB_OVERRIDES: dict[str, str] = {}


def template_context(path: Path, overrides: dict | None = None) -> dict:
    """The context `path` renders with here: `lib.render_context` plus `SHELL_STUB_OVERRIDES`.

    A render without a role's own defaults is not the script the host runs. `StubUndefined`
    iterates empty, so `{% for peer in artifacts_peer_sources %}` dropped the whole body of
    `sync-artifacts.sh.j2` — ~45 of its 100 lines, including every rsync call — and both
    linters swept the remainder as if the loop were not there (#3178). `render_context` lays
    the defaults under the inventory and expands them.

    Exposed so `ansible/tests/_shell_render.py` renders a script with the context this gate
    lints it in — one render, two consumers, no second stub set to drift (#3178). `overrides`
    go over the stubs, for a guard that needs a value the inventory does not hold.
    """
    overrides = {**SHELL_STUB_OVERRIDES, **(overrides or {})}
    component = path.parents[1]
    if component.parent == COMPONENTS:
        spec = yaml_fast.safe_load((component / "component.yml").read_text()) or {}
        return render_context(
            None,
            component_host(component.name),
            overrides=overrides,
            component_defaults={
                **role_defaults(component),
                **(spec.get("defaults") or {}),
            },
        )
    return render_context(path, overrides=overrides)


def component_host(name: str) -> str | None:
    """The first host, by name, whose `package.yml` lists component `name`, else None."""
    for spec in sorted(COMPONENTS.parent.glob("*/package.yml")):
        if name in (yaml_fast.safe_load(spec.read_text()) or {}).get("components", []):
            return spec.parent.name
    return None


def discover_templates() -> list[Path]:
    """Return every *.sh.j2 under ansible/roles/ and under a host component's templates/."""
    return sorted(ROLES.rglob("*.sh.j2")) + sorted(
        COMPONENTS.glob("*/templates/*.sh.j2")
    )


def check_template(
    path: Path,
    ctx: dict,
    out_dir: Path,
    shellcheck_bin: str | None,
) -> str | None:
    """Render one template, write it under out_dir, and lint the rendered file.

    Writes the rendered file at `path`'s repo-relative path, or its bare name for a template
    outside the repo, minus the trailing .j2.
    `shellcheck_bin=None` skips the per-file shellcheck step: main() passes None and runs
    `shellcheck_batch` over every rendered file afterwards, which is the same check in one
    process. A caller checking a single template passes the binary and gets it inline.

    Returns:
        An error string, or None on success.
    """
    rel = path.relative_to(REPO) if path.is_relative_to(REPO) else Path(path.name)
    rendered, err = render_shipped(path, ctx)
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

    return None


def main(
    argv: Sequence[str] = (), which: Callable[[str], str | None] = shutil.which
) -> int:
    """Render every discovered shell template, then `bash -n` and shellcheck the output.

    Args:
        argv: The command-line arguments. `-v` or `--verbose` also lists each clean
            template as an `[ok]` line; by default only failures and the summary print.
        which: the PATH lookup used to find shellcheck. A parameter rather than a module
            attribute so a test can prove the fail-closed branch without patching `shutil`.

    Returns:
        0 if every template rendered clean and passed both linters, 1 otherwise (including
        when shellcheck is missing from PATH or no templates were found).
    """
    answer_help(__doc__, list(argv))
    ok = ok_printer(argv)
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
        print(
            f"No *.sh.j2 templates found under {ROLES} or {COMPONENTS}", file=sys.stderr
        )
        return 1

    failures = 0
    with tempfile.TemporaryDirectory(prefix="validate-shell-templates-") as tmp:
        out_dir = Path(tmp)
        # Render + `bash -n` per template; shellcheck once over everything
        # that got that far (see shellcheck_batch for why one process, not one per file).
        rendered_ok: dict[Path, Path] = {}
        for path in templates:
            rel = path.relative_to(REPO)
            err = check_template(path, template_context(path), out_dir, None)
            if err:
                failures += 1
                print(f"  [FAIL] {rel}: {err}", file=sys.stderr)
            else:
                rendered_ok[out_dir / path.relative_to(REPO).with_suffix("")] = path
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
                ok(f"  [ok]   {rel}")

    print(f"\n{len(templates)} shell template(s) checked, {failures} failure(s).")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
