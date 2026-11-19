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

Usage: render_templates.py [-v]

`-v` / `--verbose` also lists each clean template as an `[ok]` line; by default only failures
and the summary print. Run directly or via the ``validate-render-templates`` prek hook. Exits 1
when any family reports a failure, 0 otherwise.
"""

import sys
from collections.abc import Callable, Sequence

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from lib.cli_help import answer_help
from validate.render_lib import compose, setup_plane, shell, units
from validate.validate_lib.ok_lines import ok_printer

# A family takes the `[ok]` printer and returns (checked, failures), printing its own `[FAIL]`
# lines as it goes.
Family = Callable[[Callable[[str], None]], tuple[int, int]]

FAMILIES: dict[str, Family] = {
    "compose": compose.run,
    "shell": shell.run,
    "units": units.run,
    "setup": setup_plane.run,
}


def main(argv: Sequence[str] = (), families: dict[str, Family] | None = None) -> int:
    """Run every family and print one summary line.

    Args:
        argv: The command-line arguments; see the module docstring.
        families: The families to run, `FAMILIES` by default. A test passes its own.

    Returns:
        1 if any family reported a failure, 0 otherwise.
    """
    answer_help(__doc__, list(argv))
    ok = ok_printer(argv)
    counts = {name: run(ok) for name, run in (families or FAMILIES).items()}
    checked = sum(n for n, _ in counts.values())
    failures = sum(f for _, f in counts.values())
    per_family = ", ".join(
        f"{name} {n}" + (f" ({f} failed)" if f else "")
        for name, (n, f) in counts.items()
    )
    print(f"\n{checked} template(s) checked ({per_family}), {failures} failure(s).")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
