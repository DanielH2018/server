"""A `manifests` line from a guard-map-only change waits only on the callers whose row moved (#5106).

`land.sh` narrows a change to `ansible/roles/k8s/manifests/files/standalone_secret_keys.json`
alone to the tags whose row moved (`land_shared.guard_map_rows`), because the guard task
reads only `.get(manifests_service, [])`. The deployer's discharge waited on every
`manifests` caller instead, so the line #5096 wrote outlived its landing. The test drives
`deploy_k8s_owed.discharge_k8s_unapplied` against a throwaway checkout with real commits, so
a fix that diffs the line's range in `config.repo` reads a real map at each end. No caller
has a matching render at or after the origin, so a fix confined to the render path stays red
here. In the incident no render had reached the origin yet, and the line was cleared by hand
before one did (#5118).

The last tests drive two ticks across a render-record overwrite: the line stores each caller's
matching render as its base, so a later render that does not match leaves the proof standing
(#5122).

Run: uv run pytest ansible/roles/setup/gitops_deploy/tests/test_k8s_unapplied_guard_map.py
"""

import contextlib
import dataclasses
import io
import json
import pathlib

import shared_role_callers
from deployer_lib import deploy_k8s_owed
from deployer_lib.deploy_narrow import SharedCallers
from deployer_lib.deploy_toolbox import DeployTools
from gitops_ledger import OWED_K8S_UNAPPLIED
from lib.git_testing import commit, init_repo

GUARD_MAP = "ansible/roles/k8s/manifests/files/standalone_secret_keys.json"
# Tag names absent from the real tree, so a fix that reads the real map stays red.
MOVED, UNTOUCHED = "gm-moved", "gm-untouched"


def _guard_map(moved_keys: list[str]) -> str:
    return json.dumps({MOVED: moved_keys, UNTOUCHED: ["untouched_email"]}, indent=2)


def _checkout(root: pathlib.Path) -> str:
    """Two `manifests` callers, each with a guard-map row. Returns the base commit."""
    entries = "".join(
        f"  - name: {tag}\n    platform: k8s\n" for tag in (MOVED, UNTOUCHED)
    )
    files = {
        "ansible/inventory/host_vars/daniel-box.yml": "containers_list:\n" + entries,
        "ansible/roles/k8s/manifests/tasks/main.yml": (
            "- ansible.builtin.debug:\n    msg: render\n"
        ),
        GUARD_MAP: _guard_map(["moved_email"]),
    }
    for tag in (MOVED, UNTOUCHED):
        files[f"ansible/roles/k8s/{tag}/tasks/main.yml"] = (
            "- ansible.builtin.include_role:\n    name: k8s/manifests\n"
        )
    init_repo(root)
    return commit(root, "two callers of manifests", **files)


def _callers_from_main(repo, roles, *_args, **_kwargs) -> SharedCallers:
    """`deploy_narrow.shared_role_callers`'s decode of `main`, run in-process, not over uv."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert shared_role_callers.main([*sorted(roles), "--repo", repo]) == 0
    printed = json.loads(out.getvalue())
    return SharedCallers(
        {role: set(tags) for role, tags in printed["callers"].items()},
        set(printed["owned_only"]),
    )


def test_a_map_only_line_discharges_once_the_callers_whose_row_moved_carry_it(
    gitops_deploy, settings, state, tmp_path
):
    """FLAGGED half: only `gm-moved`'s row moved and it deployed, so the line drops while
    `gm-untouched` still runs at the base commit with no matching render.

    The kept half follows: a second map-only change to the same row, with `gm-moved` behind
    it, must keep its line.
    """
    root = tmp_path / "checkout"
    base = _checkout(root)
    first = commit(
        root,
        "add a key to gm-moved's row",
        **{GUARD_MAP: _guard_map(["moved_email", "moved_token"])},
    )
    config = dataclasses.replace(settings, repo=str(root))
    records = {MOVED: first, UNTOUCHED: base}

    def discharge():
        tools = DeployTools(
            discord_post=lambda _webhook, _content: True,
            release_commit=records.get,
            shared_role_callers=_callers_from_main,
            render_proof=lambda _svc: None,
            digest_provable=lambda _repo, _roles: set(),
            digest_covers=lambda _repo, _items: set(),
        )
        return deploy_k8s_owed.discharge_k8s_unapplied(tools, state, config)

    state.record_owed(OWED_K8S_UNAPPLIED, first, {"manifests"}, 1000.0)
    assert discharge() == ["manifests"]
    assert state.owed_pending(OWED_K8S_UNAPPLIED) == []

    second = commit(
        root,
        "add another key to gm-moved's row",
        **{GUARD_MAP: _guard_map(["moved_email", "moved_token", "moved_url"])},
    )
    state.record_owed(OWED_K8S_UNAPPLIED, second, {"manifests"}, 2000.0)
    assert discharge() == []
    assert [e.service for e in state.owed_pending(OWED_K8S_UNAPPLIED)] == ["manifests"]


def test_a_range_that_also_changes_the_role_keeps_the_line_for_an_unmoved_row(
    gitops_deploy, settings, state, tmp_path
):
    """KEPT: the map moved only `gm-moved`'s row, but the same range edits `manifests`'
    tasks, so `gm-untouched` at the base commit has not run the role as the origin does."""
    root = tmp_path / "checkout"
    base = _checkout(root)
    origin = commit(
        root,
        "add a key and change the role",
        **{
            GUARD_MAP: _guard_map(["moved_email", "moved_token"]),
            "ansible/roles/k8s/manifests/tasks/main.yml": (
                "- ansible.builtin.debug:\n    msg: render twice\n"
            ),
        },
    )
    tools = DeployTools(
        discord_post=lambda _webhook, _content: True,
        release_commit={MOVED: origin, UNTOUCHED: base}.get,
        shared_role_callers=_callers_from_main,
        render_proof=lambda _svc: None,
        digest_provable=lambda _repo, _roles: set(),
        digest_covers=lambda _repo, _items: set(),
    )
    config = dataclasses.replace(settings, repo=str(root))
    state.record_owed(OWED_K8S_UNAPPLIED, origin, {"manifests"}, 1000.0)
    assert deploy_k8s_owed.discharge_k8s_unapplied(tools, state, config) == []
    assert [e.service for e in state.owed_pending(OWED_K8S_UNAPPLIED)] == ["manifests"]


def test_a_role_change_a_render_already_proved_does_not_hold_a_later_map_only_line(
    gitops_deploy, settings, state, tmp_path
):
    """FLAGGED: #5096's shape. A tasks change lands before the map-only commit, and
    `gm-untouched` was never redeployed for it: its record is at the base, and only its
    render, at the tasks commit, proves it carries that change. The map-only line must still
    drop once `gm-moved` deploys, because the render bounds the range from below."""
    root = tmp_path / "checkout"
    base = _checkout(root)
    tasks = commit(
        root,
        "change the role",
        **{
            "ansible/roles/k8s/manifests/tasks/main.yml": (
                "- ansible.builtin.debug:\n    msg: render twice\n"
            )
        },
    )
    origin = commit(
        root,
        "add a key to gm-moved's row",
        **{GUARD_MAP: _guard_map(["moved_email", "moved_token"])},
    )

    def discharge(renders):
        tools = DeployTools(
            discord_post=lambda _webhook, _content: True,
            release_commit={MOVED: origin, UNTOUCHED: base}.get,
            shared_role_callers=_callers_from_main,
            render_proof=renders.get,
            digest_provable=lambda _repo, _roles: set(),
            digest_covers=lambda _repo, _items: set(),
        )
        config = dataclasses.replace(settings, repo=str(root))
        return deploy_k8s_owed.discharge_k8s_unapplied(tools, state, config)

    state.record_owed(OWED_K8S_UNAPPLIED, origin, {"manifests"}, 1000.0)
    assert discharge({}) == [], "from the record alone, the tasks change holds the line"
    assert discharge({UNTOUCHED: tasks}) == ["manifests"]
    assert state.owed_pending(OWED_K8S_UNAPPLIED) == []


def _two_commit_range(root: pathlib.Path) -> tuple[str, str, str]:
    """#5096's shape again: base, a role change, then a map-only change. Returns all three."""
    base = _checkout(root)
    tasks = commit(
        root,
        "change the role",
        **{
            "ansible/roles/k8s/manifests/tasks/main.yml": (
                "- ansible.builtin.debug:\n    msg: render twice\n"
            )
        },
    )
    origin = commit(
        root,
        "add a key to gm-moved's row",
        **{GUARD_MAP: _guard_map(["moved_email", "moved_token"])},
    )
    return base, tasks, origin


def _discharger(settings, state, root, records, renders, newest=None):
    """A `discharge()` reading `records`, `renders` and `newest` as they stand at each call.

    `renders` is what `render_proof` answers, the matching renders; `newest` is each tag's
    newest render commit, matching or not.
    """
    newest = {} if newest is None else newest

    def discharge():
        tools = DeployTools(
            discord_post=lambda _webhook, _content: True,
            release_commit=records.get,
            shared_role_callers=_callers_from_main,
            render_proof=renders.get,
            render_commit=newest.get,
            digest_provable=lambda _repo, _roles: set(),
            digest_covers=lambda _repo, _items: set(),
        )
        config = dataclasses.replace(settings, repo=str(root))
        return deploy_k8s_owed.discharge_k8s_unapplied(tools, state, config)

    return discharge


def test_a_render_base_survives_the_producer_overwriting_the_render_record(
    gitops_deploy, settings, state, tmp_path
):
    """FLAGGED: #5122's Verify-by. Tick 1 sees `gm-untouched`'s matching render at the tasks
    commit while `gm-moved` has not deployed. The producer then re-renders past the origin and
    that render does not match, so `render_proof` answers None. Once `gm-moved` deploys, the
    line drops on the base tick 1 stored."""
    root = tmp_path / "checkout"
    base, tasks, origin = _two_commit_range(root)
    records = {MOVED: base, UNTOUCHED: base}
    renders = {UNTOUCHED: tasks}
    discharge = _discharger(settings, state, root, records, renders)

    state.record_owed(OWED_K8S_UNAPPLIED, origin, {"manifests"}, 1000.0)
    assert discharge() == [], "gm-moved's row moved and it has not deployed"
    renders.clear()
    records[MOVED] = origin
    assert discharge() == ["manifests"]


def test_a_redeploy_retires_the_stored_render_base(
    gitops_deploy, settings, state, tmp_path
):
    """KEPT: as above, but `gm-untouched` is applied again between the ticks, here at the
    base commit as a rollback would. Its release record no longer names the commit the stored
    render was matched against, so that render no longer vouches for the live bytes."""
    root = tmp_path / "checkout"
    base, tasks, origin = _two_commit_range(root)
    records = {MOVED: base, UNTOUCHED: tasks}
    renders = {UNTOUCHED: tasks}
    discharge = _discharger(settings, state, root, records, renders)

    state.record_owed(OWED_K8S_UNAPPLIED, origin, {"manifests"}, 1000.0)
    assert discharge() == []
    renders.clear()
    records.update({MOVED: origin, UNTOUCHED: base})
    assert discharge() == []
    assert [e.service for e in state.owed_pending(OWED_K8S_UNAPPLIED)] == ["manifests"]


def test_a_kept_line_names_each_caller_it_waits_on_and_why(
    gitops_deploy, settings, state, tmp_path, capsys
):
    """#5118: a line waiting on a render the producer has not yet made logged nothing that
    told it apart from a render that never matches. A range that also changes the role holds
    both callers. The reason reads the newest render's commit whether or not it matches, so a
    non-matching render from before the change still reads as waiting on the producer."""
    root = tmp_path / "checkout"
    base = _checkout(root)
    behind = commit(root, "an unrelated commit", **{"README.md": "x\n"})
    origin = commit(
        root,
        "change the role",
        **{
            "ansible/roles/k8s/manifests/tasks/main.yml": (
                "- ansible.builtin.debug:\n    msg: render twice\n"
            )
        },
    )
    after = commit(root, "a later commit", **{"README.md": "y\n"})
    records = {MOVED: base, UNTOUCHED: base}
    # Only gm-untouched's render at `behind` matches; gm-moved's does not.
    newest = {MOVED: behind, UNTOUCHED: behind}
    discharge = _discharger(settings, state, root, records, {UNTOUCHED: behind}, newest)
    state.record_owed(OWED_K8S_UNAPPLIED, origin, {"manifests"}, 1000.0)

    def waits_line():
        assert discharge() == []
        return [
            line
            for line in capsys.readouterr().out.splitlines()
            if line.startswith("k8s_unapplied: manifests waits")
        ]

    prefix = f"k8s_unapplied: manifests waits at {origin[:8]}: "
    assert waits_line() == [
        prefix + f"2 its newest render predates the change: {MOVED}, {UNTOUCHED}"
        " — waiting on the hourly render producer"
    ]
    newest.update({MOVED: after, UNTOUCHED: after})
    assert waits_line() == [
        prefix + "2 a render at or after the change does not match what it applied: "
        f"{MOVED}, {UNTOUCHED}"
    ]
    del newest[MOVED]
    assert waits_line() == [
        prefix + "1 a render at or after the change does not match what it applied: "
        f"{UNTOUCHED}; 1 it has no render record: {MOVED}"
    ]
