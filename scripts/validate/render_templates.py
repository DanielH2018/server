#!/usr/bin/env python3
"""Render the repo's Jinja templates family by family, and check what each family renders.

`check-yaml`, shellcheck and the other file-type hooks never see a `.j2`, so a template edit that
breaks its output passes them and first fails on a host. Each family renders with stubbed
variables, so no SOPS access is needed, then checks the output with the tool that reads it:

  compose  each Pi `compose_services` entry's compose.yaml.j2: YAML, the `$$` escapes, the
           watchtower labels, cap_drop, no-new-privileges and digest pins
           (`render_lib/compose.py`)
  shell    every `*.sh.j2` under k8s/roles/ and hosts/components/: `bash -n` and shellcheck,
           failing closed without shellcheck (`render_lib/shell.py`)
  units    every systemd unit template and every unit the host packages emit:
           `systemd-analyze verify`, failing closed without it; polkit rules: `node --check`
           (`render_lib/units.py`)
  setup    every other host component template: no variable nothing defines, and YAML for the
           `*.yaml.j2` (`render_lib/setup_plane.py`)

The k8s manifests have their own entry point, `k8s_manifests.py`: it validates what
`render_tag` writes for each role rather than a template glob, and CI skips its hook by id.

Usage: render_templates.py [-v] [FILE ...]

Given files, as the ``validate-render-templates`` prek hook passes the staged ones, the run
covers only the families with one of them among their inputs, and the summary line names the
families it skipped. Given none, every family runs. `-v` / `--verbose` also lists each clean
template as an `[ok]` line; by default only failures and the summary print. Exits 1 when any
family reports a failure, 0 otherwise.
"""

import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from lib.cli_help import answer_help
from validate.render_lib import compose, setup_plane, shell, units
from validate.validate_lib.ok_lines import ok_printer

# A family's run takes the `[ok]` printer and returns (checked, failures), printing its own
# `[FAIL]` lines as it goes.
Run = Callable[[Callable[[str], None]], tuple[int, int]]


@dataclass(frozen=True)
class Family:
    """One render family: its run, and the repo paths whose change it checks.

    Attributes:
        run: Renders and checks the family's templates.
        files: Matches a repo-relative path among the family's inputs.
    """

    run: Run
    files: re.Pattern[str]


# The render's import closure and the inventory it layers in, which every family reads.
_SHARED = (
    r"hosts/lib/host_lib\.py|scripts/lib/longhorn_lib/longhorn_backups\.py|cluster/services\.yml"
    r"|scripts/renderer/(renderer_lib|template_filters)/[^/]+\.py"
    r"|scripts/deploy_tools/(chained_timeout|changed_set|kustomizations)\.py"
    r"|scripts/validate/(render_templates|validate_lib/(ok_lines|shipped_render))\.py"
    r"|inventory/(host_vars/[^/]+|group_vars/all)\.yml"
    r"|scripts/lib/(inventory_lists|guard_jinja_env|host_modules|cli_help|exit_codes|estate|git|jinja_comments"
    r"|k8s_context|k8s_lookups|k8s_role_callers|k8s_roles|k8s_yaml|render_context|render_guard"
    r"|repo_paths|role_files|yaml_fast)\.py"
)

# The Pi's compose trees, which only the compose family and the host package render read.
_COMPOSE_TREES = "pi-compose/services/"


def _family(run: Run, own: str) -> Family:
    return Family(run, re.compile(rf"^({_SHARED}|{own})$"))


FAMILIES: dict[str, Family] = {
    "compose": _family(
        compose.run,
        rf"hosts/components/{_COMPOSE_TREES}.*|k8s/templates/[^/]+\.yml\.j2|scripts/validate/render_lib/compose\.py",
    ),
    "shell": _family(
        shell.run,
        rf"hosts/components/(?!{_COMPOSE_TREES}).*|hosts/[^/]+/package\.yml"
        r"|k8s/roles/.*\.sh\.j2|scripts/validate/(validate_lib/shell_lint|render_lib/shell)\.py",
    ),
    # DECIDED: every file under hosts/, so a compose-template-only commit runs units too. The
    # family renders every host package, and the daniel-pi package renders the compose trees
    # under the host renderer's own secret rules, which the compose family does not apply. A
    # package unit may also be a static file.
    "units": _family(
        units.run,
        r"hosts/.*|k8s/roles/.*\.(service|timer|socket|rules)\.j2|scripts/validate/render_lib/units\.py",
    ),
    "setup": _family(
        setup_plane.run,
        rf"hosts/components/(?!{_COMPOSE_TREES}).*\.(j2|yml)|secrets/secret_rotation\.yml"
        r"|scripts/validate/render_lib/setup_plane\.py",
    ),
}


def main(argv: Sequence[str] = (), families: dict[str, Family] | None = None) -> int:
    """Run each family whose inputs the given files touch, and print one summary line.

    Args:
        argv: The command-line arguments; see the module docstring.
        families: The families to choose from, `FAMILIES` by default. A test passes its own.

    Returns:
        1 if any family reported a failure, 0 otherwise.
    """
    answer_help(__doc__, list(argv))
    ok = ok_printer(argv)
    paths = [arg for arg in argv if not arg.startswith("-")]
    table = families or FAMILIES
    chosen = [
        name
        for name, family in table.items()
        if not paths or any(family.files.match(path) for path in paths)
    ]
    counts = {name: table[name].run(ok) for name in chosen}
    checked = sum(n for n, _ in counts.values())
    failures = sum(f for _, f in counts.values())
    per_family = ", ".join(
        f"{name} {n}" + (f" ({f} failed)" if f else "")
        for name, (n, f) in counts.items()
    )
    skipped = [name for name in table if name not in counts]
    tail = f"; skipped {', '.join(skipped)}, no input changed" if skipped else ""
    print(
        f"\n{checked} template(s) checked ({per_family}), {failures} failure(s){tail}."
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
