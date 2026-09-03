"""run_all: the --only/--skip selection and the completeness guard, as red-proof pairs.

Neither test exercises a real validator's `main()` (that's each validator's own test
module) — these only check run_all's own dispatch and registry-completeness logic, with
each registry entry's `func` stubbed.

Run: uv run pytest scripts/validate/tests/test_run_all.py
"""

import pytest

import validate
from lib.registry import Registry, package_entry_points
from validate import run_all


def test_registry_lists_all_five_validators_with_descriptions():
    lines = run_all.REGISTRY.render_list()
    assert len(lines) == 5
    names = {line.split()[0] for line in lines}
    assert names == {"compose", "config", "grafana", "k8s", "shell"}


def test_only_and_skip_selection(monkeypatch):
    calls = []
    for entry in run_all.REGISTRY:
        monkeypatch.setattr(
            entry, "func", lambda name=entry.name: calls.append(name) or 0
        )

    rc = run_all.main(["--only", "compose,shell", "--skip", "shell"])
    assert rc == 0
    assert calls == ["compose"]


def test_unknown_only_name_is_flagged_and_nothing_runs(monkeypatch, capsys):
    calls = []
    for entry in run_all.REGISTRY:
        monkeypatch.setattr(
            entry, "func", lambda name=entry.name: calls.append(name) or 0
        )

    rc = run_all.main(["--only", "nope"])
    assert rc == 2
    assert calls == []
    assert "nope" in capsys.readouterr().err


def test_a_failing_validator_makes_main_exit_nonzero(monkeypatch):
    entry = run_all.REGISTRY.get("compose")
    monkeypatch.setattr(entry, "func", lambda: 1)
    others = [e for e in run_all.REGISTRY if e.name != "compose"]
    for e in others:
        monkeypatch.setattr(e, "func", lambda: 0)

    assert run_all.main(["--only", "compose"]) == 1


# --- completeness guard — red-proof pair ------------------------------------------------


def test_expected_modules_matches_the_five_prek_validators():
    # Non-vacuity: the literal five, not just "the registry is complete against itself".
    assert run_all.EXPECTED_MODULES == {
        "compose_templates",
        "config_templates",
        "grafana_dashboards",
        "k8s_manifests",
        "shell_templates",
    }
    run_all.REGISTRY.assert_complete(run_all.EXPECTED_MODULES)  # must not raise


def test_refresh_crd_schemas_is_a_real_sixth_main_deliberately_excluded():
    # package_entry_points sees it (it has a main()); EXPECTED_MODULES does not, because it
    # isn't a prek validator. This pins that the exclusion is a choice, not a stale census.
    census = package_entry_points(validate)
    assert "refresh_crd_schemas" in census
    assert "refresh_crd_schemas" not in run_all.EXPECTED_MODULES


def test_assert_complete_rejects_a_registry_missing_a_module():
    stub = Registry("stub")
    stub.add("compose", lambda: 0, module="compose_templates")
    with pytest.raises(AssertionError):
        stub.assert_complete(run_all.EXPECTED_MODULES)
