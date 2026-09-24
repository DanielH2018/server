"""A shared k8s role makes only its consumers stale (#2504).

The other staleness rules narrow by path SHAPE and live in `test_probe_releases_stale.py`;
`test_probe_releases_check_mode.py` holds the one that reads a diff. This one narrows by the
caller graph -- which service reaches which shared role's tasks -- so it builds repos whose
roles include each other and gets its own file.

Each rule is a clean/flagged pair, and the flagged half is what proves the narrowing did not
swallow a change the fleet has to see.

Run: uv run pytest scripts/diagnostics/tests/test_probe_releases_consumers.py
"""

from diagnostics.probe_lib import releases as pr
from diagnostics.probe_lib import releases_consumers as rc

from _release_fixtures import (
    _commit,
    _init_repo,
    _record,
    _set_origin_master,
)

_SHARED = frozenset({"game-stats-lib", "manifests"})

# terraria-stats reaches game-stats-lib the way the live role does: an `import_tasks` of the
# sibling role's file by path, which names no role at all. sonarr reaches only the renderer.
_CONSUMER_TASKS = """\
- name: Stage the shared stats module
  ansible.builtin.import_tasks: "{{ role_path }}/../game-stats-lib/tasks/stage.yml"
"""

_NON_CONSUMER_TASKS = """\
- name: Deploy sonarr to the cluster
  ansible.builtin.include_role:
    name: k8s/manifests
"""


def _repo_with_a_shared_library(tmp_path, before, after):
    """Stamp three services at `before`, move origin/master to `after`, compute staleness.

    Returns the `{service: reason}` mapping for terraria-stats, valheim-stats and sonarr.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    lib_path = "ansible/roles/k8s/game-stats-lib/files/stats_lib.py"
    tree = {
        lib_path: before,
        "ansible/roles/k8s/game-stats-lib/tasks/stage.yml": "- name: Stage\n",
        "ansible/roles/k8s/terraria-stats/tasks/main.yml": _CONSUMER_TASKS,
        "ansible/roles/k8s/valheim-stats/tasks/main.yml": _CONSUMER_TASKS,
        "ansible/roles/k8s/sonarr/tasks/main.yml": _NON_CONSUMER_TASKS,
    }
    base = _commit(repo, tree, "baseline")
    tip = _commit(repo, {lib_path: after}, "change the shared module")
    _set_origin_master(repo, tip)
    records = [
        _record("terraria-stats", commit=base),
        _record("valheim-stats", commit=base),
        _record("sonarr", commit=base),
    ]
    return pr.compute_stale(records, repo_root=repo, shared_roles=_SHARED)


def test_a_shared_librarys_files_change_marks_only_the_roles_that_stage_it(tmp_path):
    """PR #2503's red proof: `game-stats-lib/files/` reaches two services, not the fleet.

    Left fleet-wide, `Release Staleness Drift` stays DOWN listing every service for a file
    none of them embeds, and no deploy tag can clear it (#2504).
    """
    stale = _repo_with_a_shared_library(tmp_path, "VERSION = 1\n", "VERSION = 2\n")
    assert sorted(stale) == ["terraria-stats", "valheim-stats"]
    assert "game-stats-lib/files/stats_lib.py" in stale["terraria-stats"]


def test_a_service_stays_stale_for_its_own_roles_change(tmp_path):
    """The flagged half: narrowing the shared roles must not narrow a service's own role."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    path = "ansible/roles/k8s/sonarr/tasks/main.yml"
    base = _commit(repo, {path: _NON_CONSUMER_TASKS}, "baseline")
    tip = _commit(
        repo, {path: _NON_CONSUMER_TASKS + "    manifests_files: []\n"}, "edit"
    )
    _set_origin_master(repo, tip)

    stale = pr.compute_stale(
        [_record("sonarr", commit=base)], repo_root=repo, shared_roles=_SHARED
    )
    assert "sonarr/tasks/main.yml" in stale["sonarr"]


def test_a_shared_role_nothing_reaches_stays_fleet_wide(tmp_path):
    """An empty caller closure widens rather than narrows: an unseen edge is not no edge.

    The repo here holds no tasks file naming `image-builder`, so its consumer set is empty --
    and sonarr, which embeds nothing of it, must still read stale. That posture is what keeps
    a derivation this reader cannot make into a visible flag instead of a false GREEN (#947).
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    path = "ansible/roles/k8s/image-builder/templates/build-job.yaml.j2"
    base = _commit(
        repo,
        {
            path: "image: a\n",
            "ansible/roles/k8s/sonarr/tasks/main.yml": _NON_CONSUMER_TASKS,
        },
        "baseline",
    )
    tip = _commit(repo, {path: "image: b\n"}, "change the build job")
    _set_origin_master(repo, tip)

    stale = pr.compute_stale(
        [_record("sonarr", commit=base)],
        repo_root=repo,
        shared_roles=frozenset({"image-builder", "manifests"}),
    )
    assert "image-builder/templates/build-job.yaml.j2" in stale["sonarr"]


def test_the_renderer_is_never_narrowed():
    """`manifests` stays off the mapping by name, so every service keeps it on its paths."""
    consumers = rc.consumers_for(
        {"manifests", "volume-claim"},
        callers={"manifests": {"sonarr"}, "volume-claim": {"sonarr"}},
    )
    assert "manifests" not in consumers
    assert pr.role_paths_for("tdarr", {"manifests"}, consumers) == [
        "ansible/roles/k8s/tdarr/",
        "ansible/roles/k8s/manifests/",
    ]


def test_a_consumer_reached_through_another_shared_role_counts():
    """The caller edge is transitive, so the closure has to be.

    No live pair is shaped this way -- `game-stats-lib/tasks/stage.yml` only NAMES
    `k8s/volume-claim`, in a comment -- so the recursion is driven here rather than left
    unexercised. A service reaching `volume-claim` only through `game-stats-lib` runs its
    tasks just the same, and a direct-callers-only reading would call it clean.
    """
    callers = {
        "volume-claim": {"game-stats-lib"},
        "game-stats-lib": {"terraria-stats"},
    }
    consumers = rc.consumers_for(
        {"volume-claim", "game-stats-lib", "manifests"}, callers=callers
    )
    assert consumers["volume-claim"] == frozenset({"terraria-stats"})
    assert consumers["game-stats-lib"] == frozenset({"terraria-stats"})


def test_the_live_tree_attributes_the_shared_roles_to_named_members():
    """The named members this derivation must find, against the real role tree.

    A caller graph read by pattern returns an EMPTY mapping the moment the roles move or the
    task keys change, and every narrowing then widens back to the fleet with nothing saying
    so. These four names are what a silently empty graph fails on.
    """
    consumers = rc.consumers_for(pr.manifest_affecting_shared_roles())
    assert consumers["game-stats-lib"] == frozenset({"terraria-stats", "valheim-stats"})
    assert consumers["arr-notification"] == frozenset({"radarr", "sonarr"})
    assert "sonarr" not in consumers["game-stats-lib"]
    assert "sonarr" in consumers["volume-claim"]
