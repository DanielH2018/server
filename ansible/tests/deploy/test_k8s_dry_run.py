"""Guards on `k8s_dry_run`, the opt-in no-mutation mode for the k8s plane.

The mode's whole value is a negative claim — "this run changed nothing" — and every failure
mode is the same shape: the flag is set, the operator believes nothing happened, and something
did. There is no error to notice afterwards, because a partial apply looks exactly like a
successful dry run from the console.

What can silently re-arm the mutation:

  * a render task going back to a hardcoded /etc/rancher/k3s/manifests path while the apply
    reads the temp dir (or the reverse) — the dry run then validates one set of manifests and
    stages another;
  * the apply losing `--dry-run=server` — a full real deploy under a flag whose name says
    otherwise;
  * `changed_when` no longer pinned false — a dry run renders into a FRESH temp dir every time,
    so render is always `changed`; that propagates into the rollout queue and the stabilisation
    gate, which then watch a workload nothing touched;
  * `rollout restart` losing its explicit guard — same always-changed render means it fires on
    every dry run, restarting the live Deployment;
  * `verify_secret_keys` losing its guard — it `kubectl patch`es live Secrets;
  * a role gaining a cluster write outside roles/k8s/manifests without a `k8s_no_mutate`
    guard, so that role half-applies under a dry run.

The last one is checked by re-deriving the census from the role sources rather than by
comparing against a copy, so adding a `kubectl delete job` to a role fails here instead of
silently widening what dry-run claims to cover.
"""

import re
from pathlib import Path

from lib import yaml_fast
from _helpers import ALL_VARS, REPO, load_tasks
from _role_census import role_dirs, role_task_files

# The `when:`-coverage scanners, shared with test_k8s_dry_run_host_writes.py beside this file.
from _k8s_guards import (
    _GUARD_FACT,
    _guard_covered_files,
    _role_with_tasks,
    _task_chunks,
)
from _k8s_render import deploy_play

_REPO = REPO
_MANIFESTS = _REPO / "ansible/roles/k8s/manifests/tasks/main.yml"
_K8S_ROLES = _REPO / "ansible/roles/k8s"

_REAL_DIR = "/etc/rancher/k3s/manifests"
_GUARD = "not k8s_dry_run | bool"


def _named(tasks: list[dict], fragment: str) -> dict:
    for task in tasks:
        if fragment in str(task.get("name", "")):
            return task
    raise AssertionError(f"no task whose name contains {fragment!r}")


def _cmd(task: dict) -> str:
    for key in ("ansible.builtin.command", "ansible.builtin.shell"):
        mod = task.get(key)
        if isinstance(mod, dict):
            return str(mod.get("cmd", ""))
        if isinstance(mod, str):
            return mod
    return ""


def _when(task: dict) -> str:
    return str(task.get("when", ""))


def test_render_and_apply_share_one_directory_fact() -> None:
    """Rendering and applying must never name the directory independently."""
    tasks = load_tasks(_MANIFESTS)
    for fragment in ("Render manifests", "Render secret manifests"):
        dest = str(_named(tasks, fragment)["ansible.builtin.template"]["dest"])
        assert "manifests_dest_dir" in dest, (
            f"{fragment!r} renders to {dest!r} rather than manifests_dest_dir. A dry run would "
            "validate one directory and stage another. See the module docstring."
        )
    assert "manifests_dest_dir" in _cmd(_named(tasks, "Apply manifests")), (
        "the apply no longer reads manifests_dest_dir, so it applies the real staging tree "
        "while the dry run rendered elsewhere."
    )


def test_no_task_hardcodes_the_real_staging_path() -> None:
    """The real path may appear only in the fact that chooses between the two."""
    offenders = [
        t.get("name")
        for t in load_tasks(_MANIFESTS)
        if _REAL_DIR in str(t)
        and "manifests_dest_dir" not in str(t.get("ansible.builtin.set_fact", ""))
    ]
    assert not offenders, (
        f"tasks hardcode {_REAL_DIR}: {offenders}. Under k8s_dry_run these write the live "
        "staging tree, where the next `kubectl apply -f <dir>/` picks them up."
    )


def test_dry_run_renders_to_a_tempdir_and_discards_it() -> None:
    tasks = load_tasks(_MANIFESTS)
    create = _named(tasks, "throwaway render directory")
    assert "ansible.builtin.tempfile" in create, (
        "the dry-run render dir is no longer a tempfile"
    )
    assert "k8s_dry_run" in _when(create)

    discard = _named(tasks, "Discard the throwaway render directory")
    assert discard["ansible.builtin.file"]["state"] == "absent", (
        "the dry-run temp dir is no longer removed, so every dry run leaks a directory of "
        "rendered manifests — some of them Secrets."
    )


def test_apply_is_server_dry_run_under_the_flag() -> None:
    cmd = _cmd(_named(load_tasks(_MANIFESTS), "Apply manifests"))
    assert "--dry-run=server" in cmd, (
        "the apply lost --dry-run=server, so k8s_dry_run now runs a REAL deploy."
    )
    assert "k8s_dry_run" in cmd, (
        "--dry-run=server is unconditional — it would break real deploys"
    )
    assert "--dry-run=client" not in cmd, (
        "client dry-run never reaches the API server, so it validates nothing this mode "
        "exists to validate — no CRDs, no admission, no defaulting."
    )


def test_apply_reports_unchanged_under_the_flag() -> None:
    changed_when = str(
        _named(load_tasks(_MANIFESTS), "Apply manifests").get("changed_when", "")
    )
    assert "k8s_dry_run" in changed_when, (
        "changed_when no longer excludes dry-run. Dry-run stdout carries the same "
        "'created'/'configured' words as a real apply, so the run reports changed and the "
        "stabilisation gate soaks a workload nothing touched."
    )


def test_mutating_downstream_tasks_are_guarded() -> None:
    """Each of these writes to the live cluster and must carry the explicit guard.

    Explicit, not inherited from the change conditions: a dry run renders into a fresh temp dir
    every time, so `manifests_render is changed` is always true.
    """
    tasks = load_tasks(_MANIFESTS)
    must_guard = [
        "Reconcile secret keys",
        "Roll the deployment after a config change",
        "Queue the batch drain for the rollout",
        "Roll the extra deployments",
        "Queue the batch drain for the extra rollouts",
    ]
    for fragment in must_guard:
        assert _GUARD in _when(_named(tasks, fragment)), (
            f"{fragment!r} is no longer guarded by k8s_dry_run, so it mutates the live cluster "
            "on a dry run. See the module docstring."
        )


def test_prune_is_skipped_under_the_flag() -> None:
    """The prune loop reads manifests_staged, which is not registered under dry-run."""
    tasks = load_tasks(_MANIFESTS)
    for fragment in ("Find staged manifests", "Prune staged manifests"):
        assert _GUARD in _when(_named(tasks, fragment)), (
            f"{fragment!r} runs under dry-run; the prune loop would then dereference an "
            "unregistered manifests_staged and fail the play."
        )


def test_the_render_directory_fact_survives_every_tag_selection() -> None:
    """manifests_dest_dir is read by config-tagged renders AND the deploy-tagged apply.

    `[config, deploy]` looks like it covers both and does the opposite: tags union, so the task
    is skipped by `--skip-tags deploy` — the config-only form CLAUDE.md documents — and the
    renders then die on `'manifests_dest_dir' is undefined`. Only `always` survives all three selections.
    """
    tasks = load_tasks(_MANIFESTS)
    for fragment in (
        "throwaway render directory",
        "Select the render directory",
        "Discard the throwaway render directory",
    ):
        tags = _named(tasks, fragment).get("tags", [])
        assert tags == ["always"], (
            f"{fragment!r} is tagged {tags}, not ['always']. Any narrower tag is dropped by one "
            "of --tags config / --tags deploy / --skip-tags deploy."
        )


def test_namespace_apply_is_guarded() -> None:
    task = _named(deploy_play()["pre_tasks"], "Apply the workload namespace")
    assert _GUARD in _when(task), (
        "the namespace apply writes to the cluster on a dry run"
    )


def _all_vars() -> dict:
    return yaml_fast.safe_load(ALL_VARS.read_text()) or {}


def test_dry_run_defaults_off() -> None:
    assert _all_vars()["k8s_dry_run"] is False, (
        "k8s_dry_run must default false — it is opt-in. A default-true would stop every real "
        "deploy, including gitops-deploy.service, while reporting success."
    )


# Mutating verbs, as they appear after `kubectl` (optionally after `-n <ns>`). `create` and
# `delete` need the trailing space so `--create-annotation` and the like do not match.
_MUTATES = re.compile(
    r"kubectl\b[^\n]*?\b(apply|create |delete |replace|scale |rollout restart|exec -i|patch )"
)

# A plain `kubectl exec <pod> -- <cmd>` (no `-i`) is only a mutation if <cmd> itself writes.
# `exec` alone also covers read probes (tdarr's/jellyfin's device checks, VAAPI encodes to
# /dev/null, `id`/`ls`/`cat`/curl GETs, janitorr's reachability check) that must not trip this.
# Curated from what the repo's exec payloads actually do, not a bare filesystem-verb scan:
# touch/mkdir/ln/mv/cp/tee/chmod/chown/dd write to a mounted volume (tdarr's write probe,
# janitorr's leaving-soon symlink); a bare `rm` does too, but needs a word boundary so it can't
# match inside `warm`/`germ`-shaped tokens; `cscli … create|delete|add|remove` and `pihole -g`
# write to the tool's own state (crowdsec's allowlists, pihole's gravity rebuild).
_EXEC_WRITE = re.compile(
    r"\b(touch|mkdir|ln -s\w*|rm|mv|cp|tee|chmod|chown|dd)\b"
    r"|cscli \S+ (create|delete|add|remove)\b"
    r"|pihole -g\b"
)

# A plain `exec` (not `-i`) that isn't already caught by _MUTATES.
_BARE_EXEC = re.compile(r"kubectl\b[^\n]*\bexec\s+(?!-i\b)\S")


def _chunk_mutates(chunk: str) -> bool:
    if "--dry-run=client" in chunk:
        return False
    if _MUTATES.search(chunk):
        return True
    return bool(_BARE_EXEC.search(chunk) and _EXEC_WRITE.search(chunk))


def _mutates_outside_manifests(role: Path) -> bool:
    """Does this role write to the cluster from its OWN tasks?"""
    return any(
        _chunk_mutates(chunk)
        for task_file in role_task_files(role)
        for chunk in _task_chunks(task_file)
    )


def _bypasses_manifests(role: Path) -> bool:
    """A role that never includes roles/k8s/manifests applies its objects some other way."""
    main = role / "tasks/main.yml"
    return main.exists() and "manifests" not in main.read_text()


def _unguarded_mutations(role: Path) -> list[str]:
    """Every task in the role that writes to the cluster with no k8s_no_mutate guard on it.

    The guard has to sit ON the mutating task (or on the guarded include that pulled its whole
    file in), not merely somewhere in the role. A raw-text search of the file
    (`_GUARD_FACT.search(task_file.read_text())`) would match cronjob-gate's COMMENTS alone:
    deleting `when: not (k8s_no_mutate | bool)` from its `kubectl create job` would leave this
    file green while `./scripts/deploy.sh --tags configarr --dry-run` fires a real gate run and
    reconciles the live *arr stack. configarr relies on that guard instead of being refused
    outright, so this is the check that reliance rests on.

    Fail-closed in two directions. A trailing comment is stripped from the guard search, so a
    `# k8s_no_mutate` in prose credits nothing. And the rule is per-task: a role that guarded a
    `block:` rather than the tasks inside it would be reported here, even though Ansible would
    propagate that `when`. No role in this tree does that; if one is written, either move
    the guard onto the tasks or make this walker read block ancestry the way
    `_autodeploy.py::_iter_task_dicts` does.
    """
    covered = _guard_covered_files(role)
    offenders = []
    for task_file in role_task_files(role):
        if task_file.name in covered:
            continue
        guarded = _task_chunks(task_file, strip_trailing_comments=True)
        # strict=True: both lists are the same file's chunks, one comment-stripped. A stripper
        # that dropped or merged a chunk would otherwise pair guards with the wrong tasks.
        for chunk, guard_text in zip(_task_chunks(task_file), guarded, strict=True):
            if not _chunk_mutates(chunk):
                continue
            if _GUARD_FACT.search(guard_text):
                continue
            # The chunk is the whole task on one line, so the name runs to the next YAML key.
            name = re.match(r"-\s*name:\s*(.*?)(?=\s+[\w.]+:|$)", chunk)
            offenders.append(
                f"{task_file.name}: {name.group(1) if name else chunk[:60]}"
            )
    return offenders


def _guarded_at_entry(role: Path) -> bool:
    """Does the role gate its mutating tasks on a no-mutation run?

    Requires at least one mutating task, and a guard on every one of them. The "at least one"
    half is what keeps a role that applies its objects some other way — `_bypasses_manifests`,
    with nothing this file recognises as a kubectl write — from reading as guarded on an empty
    loop.
    """
    return bool(_mutates_outside_manifests(role)) and not _unguarded_mutations(role)


def test_every_role_that_mutates_outside_manifests_guards_itself() -> None:
    """Every role takes the `k8s_no_mutate` guard; there is no refusal list.

    That is the invariant to keep: a new role that mutates outside
    `roles/k8s/manifests` half-applies under a dry run unless it guards itself.
    """
    scanned = [
        role
        for role in role_dirs()
        if role.name != "manifests" and (role / "tasks").is_dir()
    ]
    # Non-vacuity: the census must keep finding the roles this rule exists for.
    assert {r.name for r in scanned} >= {
        "image-builder",
        "cronjob-gate",
    }, "the role census stopped finding the roles that mutate outside manifests"
    unguarded = sorted(
        role.name
        for role in scanned
        if (_mutates_outside_manifests(role) or _bypasses_manifests(role))
        and not _guarded_at_entry(role)
    )
    assert not unguarded, (
        f"{unguarded} apply objects outside roles/k8s/manifests without a k8s_no_mutate "
        "guard, so a dry run naming one of them half-applies. Guard tasks/main.yml on "
        "k8s_no_mutate (#2588)."
    )


def _roles_included_by_other_roles() -> set[str]:
    """Roles reachable as a dependency rather than by name on the command line."""
    included: set[str] = set()
    for role in role_dirs():
        for task_file in role_task_files(role):
            for hit in re.findall(r"name:\s*k8s/([a-z0-9-]+)", task_file.read_text()):
                included.add(hit)
    return included


def test_no_role_hides_a_dependency_the_tag_refusal_cannot_see() -> None:
    """A tag names one role and reaches its dependencies, which no tag-keyed check can see.

    This is not hypothetical: `--tags freshrss` named one role and still ran volume-claim,
    which started and removed a pod against freshrss's live Longhorn PVC. Any role reachable
    as a dependency must therefore guard itself.
    """
    assert not list(_K8S_ROLES.glob("*/meta/main.yml")), (
        "a k8s role grew a meta/main.yml — role `dependencies:` there are invisible to the "
        "include scan below, so this test would stop seeing part of the closure."
    )
    unguarded = sorted(
        name
        for name in _roles_included_by_other_roles()
        if (_K8S_ROLES / name).is_dir()
        and _mutates_outside_manifests(_K8S_ROLES / name)
        and not _guarded_at_entry(_K8S_ROLES / name)
    )
    assert not unguarded, (
        f"{unguarded} mutate the cluster and are pulled in as dependencies, so a dry run of an "
        "unrelated service reaches them. Guard tasks/main.yml on k8s_no_mutate instead."
    )


_CREATE_JOB = (
    "- name: Run a one-off gate Job\n"
    "  tags: [deploy]\n"
    "  ansible.builtin.command:\n"
    "    cmd: k3s kubectl -n ns create job widget-deploy-gate --from=cronjob/widget\n"
)


def test_a_guard_named_only_in_a_comment_does_not_excuse_a_mutating_task(
    tmp_path: Path,
) -> None:
    """`_guarded_at_entry` must not be a raw-text search over the whole file.

    cronjob-gate's comments alone would satisfy one, so deleting the `when:` from its
    `kubectl create job` would leave both derivation tests green while a real dry run created
    the Job.
    """
    commented = _role_with_tasks(
        tmp_path / "a",
        main="# guarded on k8s_no_mutate elsewhere in this role\n" + _CREATE_JOB,
    )
    assert _unguarded_mutations(commented) == ["main.yml: Run a one-off gate Job"]
    assert not _guarded_at_entry(commented)

    trailing = _role_with_tasks(
        tmp_path / "b",
        main=_CREATE_JOB.replace(
            "  tags: [deploy]\n", "  tags: [deploy]  # not k8s_no_mutate guarded\n"
        ),
    )
    assert not _guarded_at_entry(trailing)

    guarded = _role_with_tasks(
        tmp_path / "c",
        main=_CREATE_JOB.replace(
            "  tags: [deploy]\n",
            "  tags: [deploy]\n  when: not (k8s_no_mutate | bool)\n",
        ),
    )
    assert _unguarded_mutations(guarded) == []
    assert _guarded_at_entry(guarded)


def test_a_guarded_import_covers_the_file_it_pulls_in(tmp_path: Path) -> None:
    """The retired volume-claim role's shape: the guard is on the import, not on the tasks that write.

    Transitive, because seed.yml includes copy.yml and copy.yml writes too. An unguarded import
    covers nothing — that is the direction that would silently excuse a real mutation.
    """
    guarded = _role_with_tasks(
        tmp_path / "a",
        main=(
            "- name: Seed the volume\n"
            "  ansible.builtin.import_tasks: seed.yml\n"
            "  when: not k8s_no_mutate\n"
        ),
        seed="- name: Recurse\n  ansible.builtin.include_tasks: copy.yml\n",
        copy=_CREATE_JOB,
    )
    assert _unguarded_mutations(guarded) == []
    assert _guarded_at_entry(guarded)

    unguarded = _role_with_tasks(
        tmp_path / "b",
        main="- name: Seed the volume\n  ansible.builtin.import_tasks: seed.yml\n",
        seed=_CREATE_JOB,
    )
    assert _unguarded_mutations(unguarded) == ["seed.yml: Run a one-off gate Job"]
    assert not _guarded_at_entry(unguarded)


def test_no_mutate_covers_check_mode_and_dry_run() -> None:
    expr = str(_all_vars()["k8s_no_mutate"])
    assert "ansible_check_mode" in expr and "k8s_dry_run" in expr, (
        "k8s_no_mutate must cover both modes. A role guarding only one is guarded against "
        "neither in practice — image-builder was fully --check-clean and would still have "
        "built and pushed an image under a dry run."
    )


def test_wrapper_translates_dry_run_and_skips_the_lock() -> None:
    import deploy_run

    dry, plain = (deploy_run.Plan(repo_root=_REPO) for _ in range(2))
    deploy_run.parse_wrapper_flags(["--dry-run", "--tags", "n8n"], dry)
    deploy_run.parse_wrapper_flags(["--tags", "n8n"], plain)
    assert dry.args[:2] == ["-e", "k8s_dry_run=true"]
    # ansible-playbook has no --dry-run, and a run that writes nothing takes no lock.
    assert (deploy_run.exec_target(dry) or [])[:3] == ["uv", "run", "ansible-playbook"]
    assert deploy_run.exec_target(plain) is None  # locked, in process
