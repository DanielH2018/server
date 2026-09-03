"""Registry: only/skip selection and the completeness guard, each as a red-proof pair."""

from __future__ import annotations

import pytest

from lib.registry import Registry, package_entry_points

# --- add / duplicate / lookup ---------------------------------------------------------


def test_add_registers_a_lookable_entry():
    reg = Registry("t")
    reg.add("disk", lambda: 1, description="disk usage")
    assert "disk" in reg
    assert reg.get("disk").description == "disk usage"
    assert reg.names() == ["disk"]


def test_add_rejects_a_duplicate_name():
    reg = Registry("t")
    reg.add("disk", lambda: 1)
    with pytest.raises(ValueError):
        reg.add("disk", lambda: 2)


# --- only/skip selection — mirrors check.py's check_enabled semantics -----------------


def test_no_filter_enables_everything():
    reg = Registry("t")
    reg.add("disk", lambda: 1)
    reg.add("cert", lambda: 1)
    assert reg.enabled("disk")
    assert reg.enabled("cert")


def test_only_restricts_to_the_named_set():
    reg = Registry("t")
    reg.add("disk", lambda: 1)
    reg.add("cert", lambda: 1)
    only = frozenset({"disk"})
    assert reg.enabled("disk", only=only)
    assert not reg.enabled("cert", only=only)


def test_skip_excludes_even_when_named_in_only():
    reg = Registry("t")
    reg.add("disk", lambda: 1)
    only = frozenset({"disk"})
    skip = frozenset({"disk"})
    assert not reg.enabled("disk", only=only, skip=skip)


def test_selected_returns_only_the_surviving_entries():
    reg = Registry("t")
    reg.add("disk", lambda: 1)
    reg.add("cert", lambda: 1)
    reg.add("mem", lambda: 1)
    names = [
        e.name
        for e in reg.selected(only=frozenset({"disk", "mem"}), skip=frozenset({"mem"}))
    ]
    assert names == ["disk"]


def test_unknown_flags_a_name_not_in_the_registry():
    reg = Registry("t")
    reg.add("disk", lambda: 1)
    assert reg.unknown({"disk", "nope"}) == ["nope"]
    assert reg.unknown({"disk"}) == []


# --- render_list ------------------------------------------------------------------------


def test_render_list_is_sorted_with_descriptions():
    reg = Registry("t")
    reg.add("mem", lambda: 1, description="memory usage")
    reg.add("disk", lambda: 1, description="disk usage")
    lines = reg.render_list()
    assert lines == ["disk  disk usage", "mem   memory usage"]


# --- completeness guard — red-proof pair ------------------------------------------------


def test_assert_complete_accepts_a_matching_module_set():
    reg = Registry("t")
    reg.add("disk", lambda: 1, module="host")
    reg.add("mem", lambda: 1, module="host")
    reg.add("alerts", lambda: 1, module="alerts")
    reg.assert_complete({"host", "alerts"})  # must not raise


def test_assert_complete_rejects_an_unregistered_module():
    reg = Registry("t")
    reg.add("disk", lambda: 1, module="host")
    with pytest.raises(AssertionError):
        reg.assert_complete({"host", "unregistered_stub"})


# --- package_entry_points ----------------------------------------------------------------


def test_package_entry_points_finds_run_and_main_modules():
    import diagnostics.probe_lib as probe_lib

    names = package_entry_points(probe_lib)
    # Non-vacuity: the known module set, not just "found something".
    assert names == [
        "alerts",
        "arr",
        "b2_ledger",
        "ha",
        "health",
        "longhorn",
        "metrics",
        "monitors",
        "readonly_rbac",
        "releases",
        "vip_placement",
    ]
    assert "core" not in names  # core.py defines helpers, not a run/main entry point
