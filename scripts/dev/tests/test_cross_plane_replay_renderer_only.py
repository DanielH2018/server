"""The replay judges a `scripts/renderer/`-only commit with each side's own renderer (#5230).

Arm 2 of the gate refuses a renderer change that adds a `dependsOn` edge to a flux-owned tag
and moves that tag's render in the same PR, because CI renders each side with its own
renderer. The replay must count such a commit refused, not clean by construction.

The render subprocess is replaced by `_fake_render`, which reads the renderer the prepared
tree holds, so a side rendered with the wrong commit's renderer renders the wrong way.

Run: uv run pytest scripts/dev/tests/test_cross_plane_replay_renderer_only.py
"""

import re

import cross_plane_replay
from cross_plane_replay import Snapshot, replay
from lib.git_testing import commit, init_repo
from lib.repo_paths import REPO
from validate import cross_plane_order

ORDERING = "scripts/renderer/renderer_lib/ordering.py"
HOST_VARS = "ansible/inventory/host_vars/daniel-box.yml"


def _fake_render(tree, roles=(), python=None):
    """A snapshot of `tree` whose bento-pdf has the edge once `tree`'s `ordering.py` says so.

    The edge and the moved render both follow the renderer file the tree holds, which is what
    distinguishes the parent's renderer from the commit's.
    """
    edge = "EDGE = True" in (tree / ORDERING).read_text()
    return Snapshot(
        renders={"bento-pdf": "b" if edge else "a", "authelia": "x"},
        owners={"bento-pdf": "flux", "authelia": "flux"},
        deps={"bento-pdf": ["authelia"] if edge else [], "authelia": []},
    )


def _rebound(fn, repo):
    """`fn`'s defaults with `REPO` replaced by `repo`."""
    return tuple(repo if d is REPO else d for d in fn.__defaults__)


def _point_at(monkeypatch, repo):
    """The replay and the gate read `repo` where they read `REPO`, and render with the fake.

    A default argument is bound when its function is defined, so the module constant alone
    does not move `changes`, `extract`, `prepare_tree` or `render_side`.
    """
    monkeypatch.setattr(cross_plane_replay, "REPO", repo)
    monkeypatch.setattr(cross_plane_order, "REPO", repo)
    monkeypatch.setattr(
        cross_plane_replay.prepare_tree,
        "__defaults__",
        _rebound(cross_plane_replay.prepare_tree, repo),
    )
    monkeypatch.setattr(
        cross_plane_order.extract,
        "__defaults__",
        _rebound(cross_plane_order.extract, repo),
    )
    monkeypatch.setattr(
        cross_plane_order.changes,
        "__defaults__",
        _rebound(cross_plane_order.changes, repo),
    )
    monkeypatch.setattr(
        cross_plane_order.render_side,
        "__defaults__",
        _rebound(cross_plane_order.render_side, repo),
    )
    monkeypatch.setattr(cross_plane_replay, "snapshot", _fake_render)
    monkeypatch.setattr(cross_plane_order, "snapshot", _fake_render)


def test_a_renderer_only_commit_that_adds_an_edge_and_moves_the_render_is_refused(
    tmp_path, monkeypatch, capsys
):
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


ROLE = "ansible/roles/k8s/bento-pdf/templates/deployment.yaml.j2"
# Today's tree holds this file and the mixed commit's own tree does not, so only that tree
# fails to render.
LATER = "ansible/later.txt"


def _fake_render_needing_later(tree, roles=(), python=None):
    """A snapshot whose bento-pdf has the edge once `tree`'s role template says so.

    Raises:
        RenderFailed: `tree` lacks `LATER`, as an older tree lacks what today's env needs.
    """
    if not (tree / LATER).exists():
        raise cross_plane_order.RenderFailed(f"{tree} predates {LATER}")
    edge = "edge" in (tree / ROLE).read_text()
    return Snapshot(
        renders={"bento-pdf": "b" if edge else "a", "authelia": "x"},
        owners={"bento-pdf": "flux", "authelia": "flux"},
        deps={"bento-pdf": ["authelia"] if edge else [], "authelia": []},
    )


def test_a_mixed_commit_whose_own_tree_does_not_render_falls_back_to_the_reversed_render(
    tmp_path, monkeypatch, capsys
):
    """A commit changing the renderer and a role keeps the reversed render for the role."""
    repo = init_repo(tmp_path / "repo")
    root = commit(
        repo,
        "base",
        **{
            HOST_VARS: "containers_list:\n"
            "  - name: bento-pdf\n    platform: k8s\n"
            "  - name: authelia\n    platform: k8s\n",
            ORDERING: "EDGE = False\n",
            ROLE: "plain\n",
        },
    )
    mixed = commit(
        repo,
        "Order bento-pdf after authelia",
        **{ORDERING: "EDGE = True\n", ROLE: "edge\n"},
    )
    commit(repo, "Add a file today's render needs", **{LATER: "x\n"})
    _point_at(monkeypatch, repo)
    monkeypatch.setattr(cross_plane_replay, "snapshot", _fake_render_needing_later)
    replay(
        "1970-01-01T00:00:00Z",
        f"{root}..master",
        ["bento-pdf", "authelia"],
        1,
        tmp_path / "cache",
    )
    out = capsys.readouterr().out
    assert re.search(rf"^REFUSED {mixed[:9]} \(reversed\) ", out, re.MULTILINE), out
