"""Each `restart_on` entry in a role's defaults names only signals the renderer knows.

`restart_on` narrows which change signal stamps a restart on one rollout entry. A name that
matches nothing is never restarted, and nothing reports it.

Run: uv run pytest tests/deploy/test_restart_on_narrows_the_restart_signals.py
"""

from _helpers import load_defaults
from _role_census import role_dirs
from renderer.renderer_lib.restart import DEFAULT_TRIGGERS

_SIGNALS = DEFAULT_TRIGGERS

# Roles known to narrow. Keeps the census non-vacuous: a renamed variable would leave it
# iterating nothing and passing.
_EXPECTED_NARROWERS = frozenset({"observability", "karakeep", "flux"})


def _narrowing_roles():
    """{role: {workload name: restart_on}} for every k8s role that narrows an entry."""
    found = {}
    for role_dir in role_dirs():
        narrowed = {
            entry["name"]: entry["restart_on"]
            for value in (load_defaults(role_dir) or {}).values()
            if isinstance(value, list)
            for entry in value
            if isinstance(entry, dict) and "name" in entry and "restart_on" in entry
        }
        if narrowed:
            found[role_dir.name] = narrowed
    return found


def test_every_narrowed_entry_names_only_real_signals():
    narrowers = _narrowing_roles()
    assert set(narrowers) >= _EXPECTED_NARROWERS, (
        f"no restart_on found for {sorted(_EXPECTED_NARROWERS - set(narrowers))}. The census "
        "reads role defaults; a renamed variable leaves it "
        "checking nothing."
    )
    for role, entries in sorted(narrowers.items()):
        for name, triggers in sorted(entries.items()):
            unknown = set(triggers) - _SIGNALS
            assert not unknown, (
                f"{role}'s {name} declares restart_on: {triggers}, and {sorted(unknown)} is not "
                f"a signal the renderer knows ({sorted(_SIGNALS)}). A name that matches "
                "nothing is never restarted, and nothing reports it."
            )
