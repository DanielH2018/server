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
  k8s      every file `render_tag` writes for a k8s role in stub mode: YAML, the vendored
           OpenAPI and CRD schemas, and the joins across the whole tree (`render_lib/k8s.py`)

Usage: render_templates.py [-v] [FILE ...]

Given files, as the ``validate-render-templates`` prek hook passes the staged ones, the run
covers only the families with one of them among their inputs, and the summary line names the
families it skipped. Given none, every family runs. `-v` / `--verbose` also lists each clean
template as an `[ok]` line; by default only failures and the summary print. Exits 1 when any
family reports a failure, 0 otherwise.

Environment:
  RENDER_TEMPLATES_SKIP  Comma-separated families to leave out whatever the files select. CI's
                         prek sweep names `k8s`, which its pytest job runs over the same tree.
                         A name that is no family fails the run.
"""

import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from lib.cli_help import answer_help
from validate.render_lib import compose, k8s, setup_plane, shell, units
from validate.validate_lib.ok_lines import ok_printer

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
    r"hosts/lib/host_lib\.py|scripts/lib/longhorn_lib/longhorn_backups\.py|k8s/services\.yml"
    r"|scripts/renderer/(renderer_lib|template_filters)/[^/]+\.py"
    r"|scripts/deploy_tools/(chained_timeout|changed_set|kustomizations)\.py"
    r"|scripts/validate/(render_templates|validate_lib/(ok_lines|shipped_render))\.py"
    r"|(hosts/[^/]+/vars|hosts/vars|estate/vars|k8s/vars)\.yml"
    r"|scripts/lib/(container_entry|inventory_lists|guard_jinja_env|cli_help|exit_codes|estate"
    r"|git|jinja_comments|k8s_conditions|k8s_context|k8s_lookups|k8s_objects|k8s_role_callers|k8s_roles|k8s_yaml"
    r"|host_modules|prom_duration|render_context|render_guard|repo_paths|role_files|yaml_fast)\.py"
)

# A k8s role's defaults, which a render of any of the role's templates layers in.
_ROLE_DEFAULTS = r"k8s/roles/[^/]+/defaults/main\.yml"

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
        rf"|k8s/roles/.*\.sh\.j2|{_ROLE_DEFAULTS}"
        r"|scripts/validate/(validate_lib/(shell_lint|host_specs)|render_lib/shell)\.py",
    ),
    # DECIDED: every file under hosts/, so a compose-template-only commit runs units too. The
    # family renders every host package, and the daniel-pi package renders the compose trees
    # under the host renderer's own secret rules, which the compose family does not apply. A
    # package unit may also be a static file.
    "units": _family(
        units.run,
        rf"hosts/.*|k8s/roles/.*\.(service|timer|socket|rules)\.j2|{_ROLE_DEFAULTS}"
        r"|scripts/validate/(validate_lib/host_specs|render_lib/units)\.py",
    ),
    "setup": _family(
        setup_plane.run,
        rf"hosts/components/(?!{_COMPOSE_TREES}).*\.(j2|yml)|secrets/secret_rotation\.yml"
        r"|scripts/validate/(validate_lib/host_specs|render_lib/setup_plane)\.py",
    ),
    # Every `.j2` under a role, not only templates/: a manifest embeds a script or config from
    # anywhere in the role with `lookup()`.
    "k8s": _family(
        k8s.run,
        rf"k8s/roles/.*\.j2|{_ROLE_DEFAULTS}|k8s/templates/ingressroute\.yml\.j2"
        r"|scripts/renderer/shadow_diff\.py|scripts/lib/plain_scalar\.py"
        r"|scripts/validate/(render_lib/k8s|validate_lib/(k8s_net_rules|k8s_schema))\.py"
        r"|scripts/validate/schemas/(kubernetes\.io|traefik\.io)/.*\.json",
    ),
}

SKIP_ENV = "RENDER_TEMPLATES_SKIP"


def main(
    argv: Sequence[str] = (),
    families: dict[str, Family] | None = None,
    environ: Mapping[str, str] = os.environ,
) -> int:
    """Run each family whose inputs the given files touch, and print one summary line.

    Args:
        argv: The command-line arguments; see the module docstring.
        families: The families to choose from, `FAMILIES` by default. A test passes its own.
        environ: Where `RENDER_TEMPLATES_SKIP` is read. A test passes its own.

    Returns:
        1 if any family reported a failure, 0 otherwise.
    """
    answer_help(__doc__, list(argv))
    ok = ok_printer(argv)
    paths = [arg for arg in argv if not arg.startswith("-")]
    table = families or FAMILIES
    held = [name for name in environ.get(SKIP_ENV, "").split(",") if name]
    unknown = [name for name in held if name not in table]
    if unknown:
        print(
            f"{SKIP_ENV} names no family: {', '.join(unknown)}. "
            f"The families are {', '.join(table)}.",
            file=sys.stderr,
        )
        return 1
    chosen = [
        name
        for name, family in table.items()
        if name not in held
        and (not paths or any(family.files.match(path) for path in paths))
    ]
    counts = {name: table[name].run(ok) for name in chosen}
    checked = sum(n for n, _ in counts.values())
    failures = sum(f for _, f in counts.values())
    per_family = ", ".join(
        f"{name} {n}" + (f" ({f} failed)" if f else "")
        for name, (n, f) in counts.items()
    )
    unchanged = [name for name in table if name not in counts and name not in held]
    tail = f"; skipped {', '.join(unchanged)}, no input changed" if unchanged else ""
    if held:
        tail += f"; skipped {', '.join(held)}, {SKIP_ENV}"
    print(
        f"\n{checked} template(s) checked ({per_family}), {failures} failure(s){tail}."
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
