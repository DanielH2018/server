"""Staleness narrowed by reading a diff: a `tasks/` change that only gates `--check` (#2416).

Every other staleness rule is a question about a path, and lives in
`test_probe_releases_stale.py` beside the #1636/#1672 narrowings it belongs with. This one has
to open the diff -- `releases_diff` is the module, and its docstring says why a path cannot
settle it -- so it gets its own repos and its own file.

Each rule is an `..._is_clean` / `..._is_flagged` pair, and the flagged half is what proves the
narrowing did not swallow a byte-moving change.

Run: uv run pytest scripts/diagnostics/tests/test_probe_releases_check_mode.py
"""

from diagnostics.probe_lib import releases as pr

from _release_fixtures import (
    _commit,
    _init_repo,
    _record,
    _set_origin_master,
)


# The fixture is tdarr's `tasks/main.yml` either side of 7fd4189cb, copied from that commit:
# the whole diff is a comment and a `when: not ansible_check_mode` gate. Copied rather than
# read from history because CI checks out at depth 1, and hand-written because a filter that
# finds its subject by path pattern passes vacuously over an empty candidate set -- this is
# the named member the filter has to catch.

_TDARR_TASKS = """\
- name: Deploy tdarr to the cluster
  ansible.builtin.include_role:
    name: k8s/manifests
  vars:
    manifests_service: tdarr
    manifests_files:
      - deployment.yaml

- name: Prove the GPU and the library are reachable from inside the pod
  tags: [deploy]
  ansible.builtin.include_tasks: verify.yml
"""

_TDARR_TASKS_CHECK_MODE_GATED = (
    _TDARR_TASKS
    + """\
  # Every proof in verify.yml is about the pod this deploy rolled out. --check rolls nothing
  # out and skips the pod lookup, so the proofs would exec against nothing and their reports
  # would error on check-mode skip results. Same gate as jellyfin's (#2351).
  when: not ansible_check_mode
"""
)


def _tasks_range(tmp_path, before, after, role="tdarr"):
    """Stamp `role` at a commit holding `before`, move origin/master to `after`, compute."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    path = f"ansible/roles/k8s/{role}/tasks/main.yml"
    base = _commit(repo, {path: before}, "baseline")
    tip = _commit(repo, {path: after}, "the change under test")
    _set_origin_master(repo, tip)
    return pr.compute_stale(
        [_record(role, commit=base)],
        repo_root=repo,
        shared_roles={"volume-claim", "manifests"},
    )


def test_a_check_mode_only_tasks_change_is_clean(tmp_path):
    """7fd4189cb's tdarr hunk renders identical manifests, so it must not read stale.

    Left counting, the Release Staleness tile goes red once the grace window passes and
    stays red until someone runs `--tags tdarr` by hand for a no-op -- and a red tile
    carrying a no-op entry hides a genuinely stale service added to it later.
    """
    assert _tasks_range(tmp_path, _TDARR_TASKS, _TDARR_TASKS_CHECK_MODE_GATED) == {}


def test_a_tasks_change_that_moves_manifest_bytes_is_flagged(tmp_path):
    """The claim size a service passes to `k8s/volume-claim` carries no `manifests_` token.

    Which is why the narrowing reads the diff for inert LINES rather than for a var-name
    match: `volume_claim_size` is read by `volume-claim/templates/pvc.yaml.j2`, so moving it
    moves the applied PVC, and a `manifests_*`-token rule would call this clean.
    """
    before = "    volume_claim_size: 1Gi\n" + _TDARR_TASKS
    after = "    volume_claim_size: 2Gi\n" + _TDARR_TASKS
    stale = _tasks_range(tmp_path, before, after)
    assert "tdarr/tasks/main.yml" in stale["tdarr"]


def test_a_check_mode_line_beside_a_real_one_is_flagged(tmp_path):
    """One unrecognised changed line keeps the whole file counting."""
    after = _TDARR_TASKS_CHECK_MODE_GATED.replace("deployment.yaml", "service.yaml")
    stale = _tasks_range(tmp_path, _TDARR_TASKS, after)
    assert "tdarr/tasks/main.yml" in stale["tdarr"]


def test_the_manifest_renderers_own_tasks_are_never_narrowed(tmp_path):
    """`manifests/tasks/` IS the render; it stays the one path that must never read clean."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    path = "ansible/roles/k8s/manifests/tasks/main.yml"
    base = _commit(repo, {path: _TDARR_TASKS}, "baseline")
    tip = _commit(repo, {path: _TDARR_TASKS_CHECK_MODE_GATED}, "gate the renderer")
    _set_origin_master(repo, tip)

    stale = pr.compute_stale(
        [_record("tdarr", commit=base)], repo_root=repo, shared_roles={"manifests"}
    )
    assert "manifests/tasks/main.yml" in stale["tdarr"]
