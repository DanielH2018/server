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
    _run_git,
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


# authelia's half of 7fd4189cb, the shape no line match covers (#2436): one task's scalar
# `when:` becomes the list form with the gate as a second conjunct, and another task's existing
# list gains the gate as an item. Both render identical manifests on a real deploy.
_AUTHELIA_TASKS = """\
- name: Read back the operator digest
  ansible.builtin.set_fact:
    authelia_password_hash: "{{ authelia_k8s_hash.stdout }}"
  when: authelia_password_hash is not defined
  no_log: true

- name: Read back the claude-ui digest
  ansible.builtin.set_fact:
    authelia_claude_password_hash: "{{ authelia_k8s_claude_hash.stdout }}"
  when:
    - authelia_k8s_manage_claude_user | bool
    - authelia_claude_password_hash is not defined
  no_log: true
"""

_AUTHELIA_TASKS_CHECK_MODE_GATED = """\
- name: Read back the operator digest
  ansible.builtin.set_fact:
    authelia_password_hash: "{{ authelia_k8s_hash.stdout }}"
  when:
    # The generate task above is a `command`, so `--check` skips it whatever its `when:` says
    # and leaves a skip result with no `stdout` (#2353).
    - not ansible_check_mode
    - authelia_password_hash is not defined
  no_log: true

- name: Read back the claude-ui digest
  ansible.builtin.set_fact:
    authelia_claude_password_hash: "{{ authelia_k8s_claude_hash.stdout }}"
  when:
    # Same reasoning as the operator digest above (#2353).
    - not ansible_check_mode
    - authelia_k8s_manage_claude_user | bool
    - authelia_claude_password_hash is not defined
  no_log: true
"""


def test_a_when_rewritten_from_scalar_to_list_is_clean(tmp_path):
    """authelia's gate, which #2416's line match could not see (#2436).

    The diff carries a removed `when: <expr>`, an added bare `when:` and added list items.
    Neither spelling is a line shape, so the rule compares the CONDITIONS either side instead:
    the same texts, plus `not ansible_check_mode`, which every real run satisfies.
    """
    stale = _tasks_range(
        tmp_path,
        _AUTHELIA_TASKS,
        _AUTHELIA_TASKS_CHECK_MODE_GATED,
        role="authelia",
    )
    assert stale == {}


def test_a_when_rewrite_that_also_changes_the_condition_is_flagged(tmp_path):
    """The discriminator between a shape test and a wrong interpreter.

    Same scalar-to-list rewrite, but one conjunct is not the one that was there. A rule that
    called this inert would be reading "a `when:` was reshaped" instead of "the conditions did
    not move", which is the #947 false-GREEN this narrowing must not become.
    """
    after = _AUTHELIA_TASKS.replace(
        "  when: authelia_password_hash is not defined\n",
        "  when:\n    - not ansible_check_mode\n    - authelia_claude_user is defined\n",
    )
    stale = _tasks_range(tmp_path, _AUTHELIA_TASKS, after, role="authelia")
    assert "authelia/tasks/main.yml" in stale["authelia"]


def test_removing_a_check_mode_gate_is_flagged(tmp_path):
    """Dropping the gate is a real change: the task runs under `--check` again.

    Only ADDING the conjunct is inert, so the comparison has to be directional.
    """
    stale = _tasks_range(
        tmp_path,
        _AUTHELIA_TASKS_CHECK_MODE_GATED,
        _AUTHELIA_TASKS,
        role="authelia",
    )
    assert "authelia/tasks/main.yml" in stale["authelia"]


def test_the_diff_context_is_pinned_against_a_hosts_git_config(tmp_path):
    """`diff.context = 0` in a host's git config must not silently widen the reading.

    A conjunct line classifies as one only with its `when:` key in view, and the key is a
    context line. Left to inherit, `diff.context = 0` would leave every candidate unclassified,
    the narrowing would no-op and every other test here would still pass -- the same
    green-and-checking-nothing shape the `--src-prefix` pin exists for.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    _run_git(repo, "config", "diff.context", "0")
    path = "ansible/roles/k8s/authelia/tasks/main.yml"
    base = _commit(repo, {path: _AUTHELIA_TASKS}, "baseline")
    tip = _commit(repo, {path: _AUTHELIA_TASKS_CHECK_MODE_GATED}, "gate the reads")
    _set_origin_master(repo, tip)

    stale = pr.compute_stale(
        [_record("authelia", commit=base)], repo_root=repo, shared_roles={"manifests"}
    )
    assert stale == {}
