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
  * a role gaining a cluster write without a `k8s_no_mutate`
    guard, so that role half-applies under a dry run.

The last one is checked by re-deriving the census from the role sources rather than by
comparing against a copy, so adding a `kubectl delete job` to a role fails here instead of
silently widening what dry-run claims to cover.
"""

import re
from pathlib import Path

from lib import yaml_fast
from _helpers import ALL_VARS, REPO
from _role_census import role_dirs, role_task_files

from _k8s_guards import (
    _GUARD_FACT,
    _chunk_mutates,
    _guard_covered_files,
    _role_with_tasks,
    _task_chunks,
)

_REPO = REPO
_K8S_ROLES = _REPO / "ansible/roles/k8s"


def _all_vars() -> dict:
    return yaml_fast.safe_load(ALL_VARS.read_text()) or {}


def test_dry_run_defaults_off() -> None:
    assert _all_vars()["k8s_dry_run"] is False, (
        "k8s_dry_run must default false — it is opt-in. A default-true would stop every real "
        "deploy, including gitops-deploy.service, while reporting success."
    )


def _mutates_outside_manifests(role: Path) -> bool:
    """Does this role write to the cluster from its OWN tasks?"""
    return any(
        _chunk_mutates(chunk)
        for task_file in role_task_files(role)
        for chunk in _task_chunks(task_file)
    )


def _bypasses_manifests(role: Path) -> bool:
    """A role whose tasks never name the manifests role applies its objects some other way."""
    main = role / "tasks/main.yml"
    return main.exists() and "manifests" not in main.read_text()


def _unguarded_mutations(role: Path) -> list[str]:
    """Every task in the role that writes to the cluster with no k8s_no_mutate guard on it.

    The guard has to sit ON the mutating task (or on the guarded include that pulled its whole
    file in), not merely somewhere in the role. A raw-text search of the file
    (`_GUARD_FACT.search(task_file.read_text())`) would match a role's COMMENTS alone:
    deleting `when: not (k8s_no_mutate | bool)` from one mutating task would leave this file
    green while a `--dry-run` fires that write for real. A role relies on that guard instead of
    being refused outright, so this is the check that reliance rests on.

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


def test_kubectl_run_is_a_mutation_and_dry_run_prose_is_not(tmp_path: Path) -> None:
    """`kubectl run --rm` still schedules a pod; qbittorrent's reach probe did on every dry run (#4569)."""
    run = (
        "- name: Reach the WebUI from a pod\n"
        "  ansible.builtin.command:\n"
        "    cmd: >-\n"
        "      k3s kubectl -n {{ k8s_namespace }}\n"
        "      run probe-{{ 4294967295 | random }} --rm -i --restart=Never --image=alpine\n"
    )
    assert _unguarded_mutations(_role_with_tasks(tmp_path / "a", main=run)) == [
        "main.yml: Reach the WebUI from a pod"
    ]
    guarded = run + "  when: not (k8s_no_mutate | bool)\n"
    assert _guarded_at_entry(_role_with_tasks(tmp_path / "b", main=guarded))

    read = (
        "- name: Read the Secret\n"
        "  # Runs under a dry run too: reads only.\n"
        "  ansible.builtin.command:\n"
        "    cmd: k3s kubectl -n {{ k8s_namespace }} get secret authelia-config\n"
    )
    assert not _mutates_outside_manifests(_role_with_tasks(tmp_path / "c", main=read))


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
    from deploy_tools.deploy_lib import run as deploy_run

    dry, plain = (deploy_run.Plan(repo_root=_REPO) for _ in range(2))
    deploy_run.parse_wrapper_flags(["--dry-run", "--tags", "n8n"], dry)
    deploy_run.parse_wrapper_flags(["--tags", "n8n"], plain)
    assert dry.args[:2] == ["-e", "k8s_dry_run=true"]
    # ansible-playbook has no --dry-run, and a run that writes nothing takes no lock.
    assert (deploy_run.exec_target(dry) or [])[:3] == ["uv", "run", "ansible-playbook"]
    assert deploy_run.exec_target(plain) is None  # locked, in process
