#!/usr/bin/env python3
"""Run one runbook's stop conditions in order, exit code naming the first failure.

A runbook whose safety is step order lists its stop conditions as gates. Each runbook's gates
are one module in runbook_gates_lib/ beside this file, and this entry point runs the one its
first argument names. `--list` prints every runbook. `runbook_gates.py <runbook> --help`
prints that runbook's gates, exit codes and arguments.

Exit codes:
  0      every gate run passed
  1..N   the first gate that failed, by the number the runbook gives it
  64     usage, including a missing or unknown runbook
  69     the cluster could not be asked

Usage:
    uv run python scripts/deploy_tools/runbook_gates.py --list
    uv run python scripts/deploy_tools/runbook_gates.py <runbook> [arguments]
"""

import importlib
import sys
from pathlib import Path as _Path

# Reach `scripts/`, for `lib` and `deploy_tools.runbook_gates_lib`: a directly-invoked script
# gets only its own directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from lib.cli_help import HELP_FLAGS
from lib.cli_registry import Registry
from lib.exit_codes import USAGE_ERROR

LIB = "deploy_tools.runbook_gates_lib"


def _runs(module: str):
    """A callable that imports `module` only when its runbook is chosen, then runs its `main`.

    Imported lazily on purpose. The weekly etcd restore drill runs `etcd-restore --gate 3` as
    root under `uv run --no-project` when the checkout has no `.venv`, so only the stdlib is
    there. `longhorn-upgrade` and `pinned-rotation` import `lib.yaml_fast`, which needs PyYAML,
    so importing every runbook up front would fail that drill.
    """

    def run(argv: list[str]) -> int:
        return importlib.import_module(f"{LIB}.{module}").main(argv)

    return run


REGISTRY = Registry("runbook_gates")
for _name, _module, _description in (
    (
        "etcd-restore",
        "etcd_restore",
        "docs/k3s-etcd-restore.md, before `systemctl stop k3s`",
    ),
    ("k3s-upgrade", "k3s_upgrade", "docs/k3s-upgrade.md, before the k3s version bump"),
    (
        "longhorn-dr",
        "longhorn_dr",
        "docs/longhorn-disaster-recovery.md, before the first restore",
    ),
    (
        "longhorn-upgrade",
        "longhorn_upgrade",
        "docs/longhorn-upgrade.md, before the Longhorn hop",
    ),
    (
        "pinned-rotation",
        "pinned_rotation",
        "docs/secret-rotation.md's pinned-secret procedure, before `change-key`",
    ),
):
    REGISTRY.add(_name, _runs(_module), _description, module=_module)


def main(argv: list[str] | None = None) -> int:
    """Run the runbook `argv[0]` names with the rest of `argv`, and return its exit code."""
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--list"]:
        print("\n".join(REGISTRY.render_list()))
        return 0
    if argv[:1] and argv[0] in HELP_FLAGS:
        print(__doc__.strip())
        return 0
    if not argv or argv[0] not in REGISTRY:
        if argv:
            print(f"unknown runbook: {argv[0]}", file=sys.stderr)
        print(__doc__.strip(), file=sys.stderr)
        print("\nRunbooks:\n  " + "\n  ".join(REGISTRY.render_list()), file=sys.stderr)
        return USAGE_ERROR
    return REGISTRY.get(argv[0]).func(argv[1:])


if __name__ == "__main__":
    sys.exit(main())
