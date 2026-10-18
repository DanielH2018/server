#!/usr/bin/env python3
"""Red-proof for observability's dashboard ConfigMap key prune.

A retired Grafana board stays live when the ConfigMap keeps a key no listed board justifies.
Server-side apply prunes a field only when the same manager owns it, so a hand-applied board or
a manager change leaves a key the apply cannot remove, and a prune that is INERT leaves it
there.

Nothing else covers it. `roles/setup/k3s/files/live_drift_check.py` EXPLICITLY excludes these
ConfigMaps from drift checking, and the prune is a `command:` chain with no `check_mode:`, so
`--check` skips it, `live_keys` falls back to `{}`, and it skips green.

So this is the paired proof the repo's own rule asks for: one fixture where an orphan MUST be
selected for deletion, and one where the sets agree and NOTHING may be selected. The second
half is the one that catches an inert prune: a prune that selects nothing on every input is
indistinguishable from a working prune, from the passing side.

WHY IT ALSO PINS THE REGISTER NAME. Rendering the expression against hand-written fixture
variables would keep passing if someone renamed the `register:` in dashboards.yml — the real
expression would then read an undefined variable, prune nothing, and this file would still be
green on its fixtures. So the name the expression reads is asserted against the name the read
task sets.

Run: uv run pytest ansible/tests/services/test_dashboard_prune.py
"""

from _helpers import ANSIBLE
from _helpers import load_tasks
from _helpers import render_expr
from _helpers import task_named

DASHBOARDS = ANSIBLE / "roles" / "k8s" / "observability" / "tasks" / "dashboards.yml"
PRUNE = "Remove dashboard keys with no board behind them"
READ = "Read the live dashboard ConfigMap keys"


def _stale_keys(live_json: str, boards: list[str]) -> list[str]:
    """`stale_keys` as dashboards.yml computes it, through the same two `vars` it derives from."""
    v = task_named(load_tasks(DASHBOARDS), PRUNE)["vars"]
    ctx = {
        "observability_dashboard_live_keys": {"results": [{"stdout": live_json}]},
        "observability_dashboard_folders": {"Apps": boards},
        "item": "Apps",
        "idx": 0,
    }
    ctx["live_keys"] = render_expr("{{ " + v["live_keys"].strip(" {}") + " }}", **ctx)
    ctx["want_keys"] = render_expr("{{ " + v["want_keys"].strip(" {}") + " }}", **ctx)
    return render_expr("{{ " + v["stale_keys"].strip(" {}") + " }}", **ctx)


def test_the_key_prune_selects_an_orphaned_configmap_key() -> None:
    stale = _stale_keys(
        live_json='{"backups.json": "...", "live.json": "..."}', boards=["live.json"]
    )
    assert stale == ["backups.json"], (
        "a live key with no listed board must be selected for deletion — server-side apply "
        "cannot remove a key another manager owns"
    )


def test_the_key_prune_is_clean_when_the_sets_agree() -> None:
    stale = _stale_keys(live_json='{"live.json": "..."}', boards=["live.json"])
    assert stale == [], (
        "the normal case must issue no patch at all — `when: stale_keys | length > 0` is what "
        "keeps this safe to run on every deploy"
    )


def test_the_key_prune_treats_a_missing_configmap_as_empty() -> None:
    """`--ignore-not-found` returns an empty stdout.

    Without the `default('{}', true)` the from_json raises and the whole deploy fails on a
    ConfigMap that does not exist yet.
    """
    assert _stale_keys(live_json="", boards=["live.json"]) == []


def test_the_prune_reads_the_register_the_read_task_sets() -> None:
    """A `register:` rename would leave the prune reading an undefined variable."""
    tasks = load_tasks(DASHBOARDS)
    register = task_named(tasks, READ).get("register")
    assert register == "observability_dashboard_live_keys"
    assert register in str(task_named(tasks, PRUNE)["vars"])


def test_the_prune_wants_the_boards_the_configmaps_render() -> None:
    """The prune reads the list the ConfigMaps render from, so a listed board is never pruned.

    test_dashboard_configmaps.py holds each rendered ConfigMap's keys to that same list.
    """
    want = str(task_named(load_tasks(DASHBOARDS), PRUNE)["vars"]["want_keys"])
    assert "observability_dashboard_folders[item]" in want


def test_the_key_prune_is_the_only_difference_based_prune() -> None:
    """A set-difference prune added later would inherit no coverage."""
    prunes = [t for t in load_tasks(DASHBOARDS) if "difference(" in str(t)]
    assert [t.get("name") for t in prunes] == [PRUNE], (
        "a new difference-based prune in dashboards.yml needs its own accept/reject pair here"
    )
