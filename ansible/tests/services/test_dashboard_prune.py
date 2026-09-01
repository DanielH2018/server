#!/usr/bin/env python3
"""Red-proof for claude-otel's three dashboard prunes.

A retired Grafana board stayed live because the ConfigMap kept a key no file justified, and #517
shipped a prune for it that was INERT — #520's own commit message says so: "Correcting #517. Its
diagnosis was wrong and the task it added was inert." #520 then shipped three more prunes, and
none of the four ever had a test.

Nothing else covers them. `test_deploy_annotations.py` asserts only that the ConfigMap is built
`--from-file` the annotated tree, and `roles/setup/k3s/files/live_drift_check.py` EXPLICITLY
excludes these ConfigMaps from drift checking. All three prunes are `command:`/`find:` chains with
no `check_mode:`, so `--check` skips them, `live_keys` falls back to `{}`, and every prune skips
green. The only evidence they work is one operator's manual observation.

So this is the paired proof the repo's own rule asks for — for each prune, one fixture where an
orphan MUST be selected for deletion, and one where the sets agree and NOTHING may be selected.
The second half is the one that would have caught #517: a prune that selects nothing on every
input is indistinguishable from a working prune, from the passing side.

WHY IT ALSO PINS THE REGISTER NAMES. Rendering these expressions against hand-written fixture
variables would keep passing if someone renamed a `register:` in dashboards.yml — the real
expression would then read an undefined variable, prune nothing, and this file would still be
green on its fixtures. That is the same failure shape as the inert prune itself, one level up, so
the names the expressions read are asserted against the names the `find:` tasks actually set.

Run: uv run pytest ansible/tests/test_dashboard_prune.py
"""

from pathlib import Path

from _helpers import ANSIBLE
from _helpers import load_tasks
from _helpers import render_expr
from _helpers import task_named


DASHBOARDS = ANSIBLE / "roles" / "k8s" / "claude-otel" / "tasks" / "dashboards.yml"
STAGED = "/etc/rancher/k3s/dashboards"
ANNOTATED = "/etc/rancher/k3s/dashboards-annotated"


def _tasks() -> list[dict]:
    return load_tasks(DASHBOARDS)


def _loop_of(fragment: str) -> str:
    """The `loop:` expression of the task whose name contains `fragment`.

    Returned verbatim — these are folded scalars that already carry their own `{{ }}`, so a
    caller must render the string as-is rather than wrapping it again.
    """
    loop = task_named(_tasks(), fragment).get("loop")
    assert isinstance(loop, str), f"{fragment!r} has no templated loop to evaluate"
    return loop


def _found(paths: list[str]) -> dict:
    """The shape `ansible.builtin.find` registers: results[].files[].path."""
    return {"results": [{"files": [{"path": p} for p in paths]}]}


# --- the staged prune -------------------------------------------------------------------------


def _staged_selection(on_disk: list[str], justified: list[str]) -> list[str]:
    return render_expr(
        _loop_of("Remove staged dashboards with no file behind them"),
        claude_otel_staged_found=_found(on_disk),
        claude_otel_valid_staged=justified,
    )


def test_the_staged_prune_selects_an_orphan() -> None:
    selected = _staged_selection(
        on_disk=[f"{STAGED}/Apps/backups.json", f"{STAGED}/Apps/live.json"],
        justified=[f"{STAGED}/Apps/live.json"],
    )
    assert selected == [f"{STAGED}/Apps/backups.json"], (
        "a staged file with no role file behind it must be selected for deletion — this is the "
        "retired-board-stays-live defect #520 was correcting"
    )


def test_the_staged_prune_is_clean_when_the_sets_agree() -> None:
    selected = _staged_selection(
        on_disk=[f"{STAGED}/Apps/live.json"],
        justified=[f"{STAGED}/Apps/live.json"],
    )
    assert selected == [], (
        "a prune that selects something when nothing is orphaned would delete a live board on "
        "every run"
    )


# --- the annotated prune ----------------------------------------------------------------------


def _annotated_selection(on_disk: list[str], justified_staged: list[str]) -> list[str]:
    return render_expr(
        _loop_of("Remove annotated dashboards with no staged source"),
        claude_otel_annotated_found=_found(on_disk),
        claude_otel_valid_staged=justified_staged,
    )


def test_the_annotated_prune_selects_an_orphan() -> None:
    selected = _annotated_selection(
        on_disk=[f"{ANNOTATED}/Apps/backups.json", f"{ANNOTATED}/Apps/live.json"],
        justified_staged=[f"{STAGED}/Apps/live.json"],
    )
    assert selected == [f"{ANNOTATED}/Apps/backups.json"], (
        "pruning only the staged copy moves the stale file one hop down the chain; the "
        "ConfigMap is built from the ANNOTATED tree, so this hop is the one that matters"
    )


def test_the_annotated_prune_is_clean_when_the_sets_agree() -> None:
    selected = _annotated_selection(
        on_disk=[f"{ANNOTATED}/Apps/live.json"],
        justified_staged=[f"{STAGED}/Apps/live.json"],
    )
    assert selected == [], "an agreeing tree must produce no deletions"


def test_the_annotated_prune_rewrites_the_staged_path_before_comparing() -> None:
    """The two trees differ only by directory name. Comparing them without the rewrite would
    find every annotated file orphaned and delete the whole tree on the first run."""
    loop = _loop_of("Remove annotated dashboards with no staged source")
    assert "dashboards-annotated" in loop and "regex_replace" in loop, (
        "the staged allow-list must be rewritten into annotated paths before the difference, or "
        "the prune deletes every annotated dashboard"
    )


# --- the live ConfigMap key prune -------------------------------------------------------------


def _stale_keys(live_json: str, source_files: list[str]) -> list[str]:
    """`stale_keys` as dashboards.yml computes it, through the same two `vars` it derives from."""
    task = task_named(_tasks(), "Remove dashboard keys with no file behind them")
    v = task["vars"]
    ctx = {
        "claude_otel_dashboard_live_keys": {"results": [{"stdout": live_json}]},
        "claude_otel_dashboard_source_files": {
            "results": [{"files": [{"path": p} for p in source_files]}]
        },
        "idx": 0,
    }
    ctx["live_keys"] = render_expr("{{ " + v["live_keys"].strip(" {}") + " }}", **ctx)
    ctx["want_keys"] = render_expr("{{ " + v["want_keys"].strip(" {}") + " }}", **ctx)
    return render_expr("{{ " + v["stale_keys"].strip(" {}") + " }}", **ctx)


def test_the_key_prune_selects_an_orphaned_configmap_key() -> None:
    stale = _stale_keys(
        live_json='{"backups.json": "...", "live.json": "..."}',
        source_files=[f"{ANNOTATED}/Apps/live.json"],
    )
    assert stale == ["backups.json"], (
        "server-side apply prunes a field only when the same manager owns it, so a hand-applied "
        "or manager-changed key is invisible to the file-to-file prunes above — this is the arm "
        "that catches it"
    )


def test_the_key_prune_is_clean_when_the_sets_agree() -> None:
    stale = _stale_keys(
        live_json='{"live.json": "..."}',
        source_files=[f"{ANNOTATED}/Apps/live.json"],
    )
    assert stale == [], (
        "the normal case must issue no patch at all — `when: stale_keys | length > 0` is what "
        "keeps this safe to run on every deploy"
    )


def test_the_key_prune_treats_a_missing_configmap_as_empty() -> None:
    """`--ignore-not-found` returns an empty stdout. Without the `default('{}', true)` the
    from_json raises and the whole deploy fails on a ConfigMap that does not exist yet."""
    assert _stale_keys(live_json="", source_files=[f"{ANNOTATED}/Apps/live.json"]) == []


# --- the guard against the guard going stale ---------------------------------------------------


def test_the_expressions_read_the_registers_the_find_tasks_set() -> None:
    """Fixture variable names are the way this file could pass while the real prunes are broken.

    A `register:` rename in dashboards.yml would leave the real expression reading an undefined
    variable — pruning nothing, silently — while every fixture above still goes red on cue. So
    each register name is asserted against the task that actually sets it.
    """
    registers = {
        "Find the staged dashboards that no longer exist in the role": "claude_otel_staged_found",
        "Find the annotated dashboards that no longer have a staged source": "claude_otel_annotated_found",
        "Read the live dashboard ConfigMap keys": "claude_otel_dashboard_live_keys",
        "Find the dashboard files each ConfigMap should carry": "claude_otel_dashboard_source_files",
    }
    for name, expected in registers.items():
        actual = task_named(_tasks(), name).get("register")
        assert actual == expected, (
            f"{name!r} registers {actual!r}, but the prune expressions this file exercises read "
            f"{expected!r} — a rename here makes the real prune read an undefined variable and "
            "select nothing, while these fixtures stay green"
        )

    consumers = "\n".join(
        [
            _loop_of("Remove staged dashboards with no file behind them"),
            _loop_of("Remove annotated dashboards with no staged source"),
            str(
                task_named(_tasks(), "Remove dashboard keys with no file behind them")[
                    "vars"
                ]
            ),
        ]
    )
    for expected in registers.values():
        assert expected in consumers, (
            f"{expected} is registered but no prune reads it — an orphaned register means one "
            "of the three prunes is no longer wired to its input"
        )


def test_the_prune_set_has_not_grown_unguarded() -> None:
    """A fourth set-difference prune added later would inherit no coverage, which is how the
    first three got here.

    Keyed on `difference(`, not on the task name: dashboards.yml also holds three one-shot
    `Remove ...` cleanups (the client-side apply directory, and the retired vendored Claude Code
    manifest and its ConfigMap) which delete a fixed path and have no set logic to prove. It is
    the difference expressions that can silently select nothing.
    """
    prunes = [t for t in _tasks() if "difference(" in str(t)]
    names = sorted(str(t.get("name")) for t in prunes)
    assert len(prunes) == 3, (
        f"dashboards.yml has {len(prunes)} difference-based prunes ({names}), expected the 3 "
        "this file proves; a new one needs its own accept/reject pair here"
    )


def test_the_pruned_tree_is_derived_not_enumerated() -> None:
    """The corpus is right — `fileglob` over the role's own files, not a hardcoded board list.
    Pinned so a future 'simplification' to a literal list cannot pass unnoticed."""
    justify = task_named(
        _tasks(), "Build the staged paths the role's own files justify"
    )
    expr = str(justify["ansible.builtin.set_fact"]["claude_otel_valid_staged"])
    assert "fileglob" in expr and "role_path" in expr, (
        "the allow-list must be globbed from the role's files/ tree; an enumerated list would "
        "silently stop justifying a board someone adds"
    )


def test_the_source_of_truth_path_is_what_the_configmap_is_built_from() -> None:
    """Ties the annotated tree this file prunes to the tree the ConfigMap is actually built from,
    so the prune cannot end up cleaning a directory nothing serves."""
    build = task_named(_tasks(), "Build a ConfigMap manifest per dashboard folder")
    assert "dashboards-annotated" in str(build), (
        "if the ConfigMap stops being built from the annotated tree, the annotated prune is "
        "cleaning a directory that no longer feeds Grafana"
    )


def test_dashboards_yml_is_where_the_prunes_live() -> None:
    """A cheap existence check, so a moved file fails here rather than silently emptying every
    assertion above through a zero-task parse."""
    assert Path(DASHBOARDS).is_file()
    assert len(_tasks()) > 5
