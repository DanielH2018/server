"""probe's REGISTRY: --list rendering and the completeness guard, as red-proof pairs.

The guard asserts every `probe_lib` module that defines a `run_*`/`main` entry point is
covered by some REGISTRY entry's `module=`. It is deliberately checked against the literal
twelve names below, not just "REGISTRY matches whatever `package_entry_points` returns
today" — see CLAUDE.md's "Python & Tests" on non-vacuity.

Run: uv run pytest scripts/diagnostics/tests/test_probe_registry.py
"""

import pytest

import probe
from diagnostics import probe_lib
from diagnostics.probe_lib import subcommands
from lib.registry import Registry, package_entry_points

# The twelve probe_lib modules that define a run_*/main entry point (core.py doesn't — it's
# helpers, not a subcommand backend). Every one of probe.py's 22 subcommands maps to one of
# these (several subcommands share a module, e.g. "monitors"/"kuma-drift" both back onto
# monitors.py, and "targets"/"pi" both back onto pi_plane.py) or to none (the streaming,
# plan()-driven subcommands like `loki-labels`/`cert`).
EXPECTED_MODULES = frozenset(
    {
        "alerts",
        "arr",
        "b2_ledger",
        "ha",
        "health",
        "longhorn",
        "metrics",
        "monitors",
        "pi_plane",
        "readonly_rbac",
        "releases",
        "vip_placement",
    }
)


def test_package_entry_points_matches_the_known_twelve():
    assert package_entry_points(probe_lib) == sorted(EXPECTED_MODULES)


def test_registry_completeness_guard_accepts_the_real_registry():
    subcommands.REGISTRY.assert_complete(EXPECTED_MODULES)  # must not raise


def test_registry_completeness_guard_rejects_an_unregistered_stub():
    stub = Registry("stub")
    stub.add("disk", None, module="host")
    with pytest.raises(AssertionError):
        stub.assert_complete(EXPECTED_MODULES)


def test_list_flag_prints_every_subcommand_with_a_description(capsys):
    assert probe.main(["--list"]) == 0
    out = capsys.readouterr().out
    lines = out.strip().splitlines()
    assert len(lines) == len(subcommands.SUBCOMMANDS) == 23
    for name, description, _module, _func in subcommands.SUBCOMMANDS:
        assert any(line.startswith(name) and description in line for line in lines), (
            name,
            description,
        )
