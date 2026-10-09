"""probe's REGISTRY: --list, dispatch, parser agreement and the completeness guard, as red-proof pairs.

The guard asserts every `probe_lib` module that defines a `run_*`/`main` entry point is
covered by some REGISTRY entry's `module=`. It is deliberately checked against the literal
thirteen names below, not just "REGISTRY matches whatever `package_entry_points` returns
today" — see CLAUDE.md's "Python & Tests" on non-vacuity.

Run: uv run pytest scripts/diagnostics/tests/test_probe_registry.py
"""

import argparse
import os
import sys

import pytest

# `scripts/diagnostics` is deliberately absent from `pythonpath` in pyproject.toml, so this
# module puts its own parent directory on `sys.path` — the insert every sibling here carries.
# (`diagnostics.probe_lib` and `lib.cli_registry` below resolve through `scripts`, which IS on
# `pythonpath`; the bare `probe` does not.)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import probe
from diagnostics import probe_lib
from diagnostics.probe_lib import subcommands
from diagnostics.probe_lib.cli_parser import _build_parser
from lib.cli_registry import Registry, package_entry_points

# The thirteen probe_lib modules that define a run_*/main entry point (core.py doesn't — it's
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
        "shed_set",
        "vip_placement",
    }
)


def test_package_entry_points_matches_the_known_thirteen():
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
    assert len(lines) == len(subcommands.SUBCOMMANDS) == 22
    for name, description, _module, _func in subcommands.SUBCOMMANDS:
        assert any(line.startswith(name) and description in line for line in lines), (
            name,
            description,
        )


def _subcommand_names(parser):
    """The top-level subcommand names an argparse parser accepts."""
    (action,) = [
        a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
    ]
    return set(action.choices)


def test_parser_subcommands_equal_the_registry_names():
    names = _subcommand_names(_build_parser())
    assert "ha-state" in names  # a named member, so an empty parse cannot pass
    assert names == set(subcommands.REGISTRY.names())


def test_parser_registry_comparison_catches_an_unregistered_subcommand():
    parser = _build_parser()
    (action,) = [
        a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
    ]
    action.add_parser("unregistered")
    assert _subcommand_names(parser) != set(subcommands.REGISTRY.names())


def test_main_dispatches_a_handler_subcommand_through_the_registry(monkeypatch):
    calls = []
    entry = subcommands.REGISTRY.get("shed-set")
    assert "handler" in entry.flags
    monkeypatch.setattr(entry, "func", lambda ns: calls.append(ns.cmd) or 7)
    assert probe.main(["shed-set"]) == 7
    assert calls == ["shed-set"]


def test_only_routed_subcommands_with_a_callable_lack_the_handler_flag():
    unflagged = {
        e.name
        for e in subcommands.REGISTRY
        if e.func is not None and "handler" not in e.flags
    }
    assert unflagged == subcommands.ROUTED_IN_MAIN


# The invocations of each `ROUTED_IN_MAIN` subcommand that must stream through `plan()` rather
# than reach the registry callable. `health` is absent: `main()` answers every `health`
# invocation before the `handlers` lookup, so its flag cannot change where it goes.
_STREAMING_INVOCATIONS = [
    ["--dry-run", "metric", "up", "--json"],
    ["--dry-run", "metric", "up"],
    ["--dry-run", "loki-query", '{app="x"}', "--json"],
    ["--dry-run", "targets"],
]


@pytest.mark.parametrize("argv", _STREAMING_INVOCATIONS, ids=" ".join)
def test_a_routed_subcommand_streams_when_its_callable_does_not_apply(
    argv, monkeypatch, capsys
):
    # Every registry callable raises, so a dispatch that reaches one fails here rather than
    # printing a plausible answer. Dropping a name from ROUTED_IN_MAIN flags it "handler",
    # which sends these invocations to the callable. The stubs cover every entry, not just
    # ROUTED_IN_MAIN, so editing that set cannot also shrink what this test guards.
    for entry in subcommands.REGISTRY:
        if entry.func is None:
            continue

        def _refuse(*_a, _name=entry.name, **_k):
            raise AssertionError(f"{_name}: reached the registry callable, not plan()")

        monkeypatch.setattr(entry, "func", _refuse)
    planned = []

    def fake_plan(args, _resolve_ip):
        planned.append(args)
        return [["curl", "planned"]]

    assert probe.main(argv, plan=fake_plan) == 0
    assert planned == [argv]
    assert capsys.readouterr().out == "curl planned\n"
