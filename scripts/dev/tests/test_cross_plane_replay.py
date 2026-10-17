"""The replay's tree preparation: the ownership rewrite, the transplant and the reversal.

Run: uv run pytest scripts/dev/tests/test_cross_plane_replay.py
"""

import inspect
import re

import cross_plane_replay
import pytest

from cross_plane_replay import Snapshot, blockers, prepare_tree, replay, set_flux_owned
from lib.git_testing import commit, init_repo
from lib.repo_paths import REPO
from validate import cross_plane_order

TEMPLATE = "ansible/templates/shell.j2"
INVENTORY = "ansible/inventory/hosts.yml"


def test_set_flux_owned_leaves_exactly_the_named_entries_on_flux(tmp_path):
    host_vars = tmp_path / "daniel-box.yml"
    host_vars.write_text(
        "containers_list:\n"
        "  - name: bento-pdf\n    platform: k8s\n"
        "  - name: texbrain\n    platform: k8s\n    owner: flux\n"
    )
    set_flux_owned(host_vars, ["bento-pdf"])
    assert host_vars.read_text() == (
        "containers_list:\n"
        "  - name: bento-pdf\n    owner: flux\n    platform: k8s\n"
        "  - name: texbrain\n    platform: k8s\n"
    )


@pytest.fixture
def repo(tmp_path):
    """A history whose middle commit edits the template; HEAD then edits the inventory."""
    repo = init_repo(tmp_path / "repo")
    commit(
        repo, "base", **{TEMPLATE: "limit: 3\n", INVENTORY: "a\n", "scripts/r.py": ""}
    )
    edit = commit(repo, "edit", **{TEMPLATE: "limit: 5\n"})
    commit(repo, "later", **{INVENTORY: "b\n"})
    return repo, edit


def test_a_transplant_takes_the_commit_s_paths_and_keeps_head_elsewhere(tmp_path, repo):
    repo, edit = repo
    tree = tmp_path / "tree"
    prepare_tree(f"{edit}^", [TEMPLATE], tree, repo=repo)
    assert (tree / TEMPLATE).read_text() == "limit: 3\n"
    assert (tree / INVENTORY).read_text() == "b\n"


def test_an_undo_reverses_only_the_commit_s_own_change(tmp_path, repo):
    repo, edit = repo
    tree = tmp_path / "tree"
    prepare_tree(edit, [TEMPLATE], tree, undo=True, repo=repo)
    assert (tree / TEMPLATE).read_text() == "limit: 3\n"
    assert (tree / INVENTORY).read_text() == "b\n"


def test_an_undo_that_no_longer_applies_is_reported(tmp_path, repo):
    repo, edit = repo
    commit(repo, "rewrite", **{TEMPLATE: "limit: 9\n"})
    assert prepare_tree(edit, [TEMPLATE], tmp_path / "tree", undo=True, repo=repo) == [
        TEMPLATE
    ]


def test_an_unreversed_file_blocks_only_on_a_flux_owned_dependency_path(tmp_path):
    snap = Snapshot(
        renders={},
        owners={"bento-pdf": "flux", "authelia": "ansible", "zigbee2mqtt": "ansible"},
        deps={"bento-pdf": ["authelia"], "authelia": [], "zigbee2mqtt": []},
    )
    paths = [
        "ansible/roles/k8s/authelia/defaults/main.yml",
        "ansible/roles/k8s/zigbee2mqtt/tasks/main.yml",
        "ansible/templates/gone.yml.j2",
    ]
    for path in paths[:2]:
        (tmp_path / path).parent.mkdir(parents=True)
        (tmp_path / path).write_text("")
    assert blockers(paths, tmp_path, snap, ["bento-pdf"]) == [paths[0]]


# -- a renderer-only commit is judged with each side's own renderer (#5230) -----------------

ORDERING = "scripts/renderer/renderer_lib/ordering.py"
HOST_VARS = "ansible/inventory/host_vars/daniel-box.yml"


def _fake_render(tree, roles=(), python=None):
    """What `tree`'s own renderer writes: bento-pdf gains its edge once `ordering.py` says so.

    It stands in for the subprocess `snapshot` runs, and reads the renderer the tree holds, so
    a side rendered with the wrong commit's renderer renders the wrong way.
    """
    edge = "EDGE = True" in (tree / ORDERING).read_text()
    return Snapshot(
        renders={"bento-pdf": "b" if edge else "a", "authelia": "x"},
        owners={"bento-pdf": "flux", "authelia": "flux"},
        deps={"bento-pdf": ["authelia"] if edge else [], "authelia": []},
    )


def _point_at(monkeypatch, repo):
    """Every function of the replay and the gate that reads `REPO` reads `repo` instead.

    A default argument is bound when its function is defined, so the module constant alone
    does not move `changes`, `extract` or `prepare_tree`.
    """
    for module in (cross_plane_replay, cross_plane_order):
        monkeypatch.setattr(module, "REPO", repo)
        for _, fn in inspect.getmembers(module, inspect.isfunction):
            defaults = fn.__defaults__ or ()
            if any(d is REPO for d in defaults):
                patched = tuple(repo if d is REPO else d for d in defaults)
                monkeypatch.setattr(fn, "__defaults__", patched)
        monkeypatch.setattr(module, "snapshot", _fake_render)


def test_a_renderer_only_commit_that_adds_an_edge_and_moves_the_render_is_refused(
    tmp_path, monkeypatch, capsys
):
    """Arm 2 refuses it in CI, so the replay must count it refused, not clean by construction."""
    repo = init_repo(tmp_path / "repo")
    root = commit(
        repo,
        "base",
        **{
            HOST_VARS: "containers_list:\n"
            "  - name: bento-pdf\n    platform: k8s\n"
            "  - name: authelia\n    platform: k8s\n",
            ORDERING: "EDGE = False\n",
        },
    )
    edge = commit(repo, "Order bento-pdf after authelia", **{ORDERING: "EDGE = True\n"})
    _point_at(monkeypatch, repo)
    replay(
        "1970-01-01T00:00:00Z",
        f"{root}..master",
        ["bento-pdf", "authelia"],
        1,
        tmp_path / "cache",
    )
    out = capsys.readouterr().out
    assert re.search(rf"^REFUSED {edge[:9]} ", out, re.MULTILINE), out
    assert "arm 2: bento-pdf gains dependsOn ['authelia']" in out
