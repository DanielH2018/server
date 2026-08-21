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
  * k8s_dry_run_unsupported drifting behind the roles that actually mutate outside
    roles/k8s/manifests — the refusal in deploy.yml stops covering a role that needs it, and
    that role half-applies.

The last one is checked by re-deriving the list from the role sources rather than by comparing
against a copy, so adding a `kubectl delete job` to a role fails here instead of silently
widening what dry-run claims to cover.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_REPO = Path(__file__).resolve().parents[2]
_MANIFESTS = _REPO / "ansible/roles/k8s/manifests/tasks/main.yml"
_DEPLOY = _REPO / "ansible/deploy.yml"
_ALL_VARS = _REPO / "ansible/inventory/group_vars/all.yml"
_K8S_ROLES = _REPO / "ansible/roles/k8s"
_DEPLOY_SH = _REPO / "scripts/deploy.sh"

_REAL_DIR = "/etc/rancher/k3s/manifests"
_GUARD = "not k8s_dry_run | bool"


def _tasks(path: Path) -> list[dict]:
    return yaml.safe_load(path.read_text()) or []


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


# --------------------------------------------------------------------------- render location


def test_render_and_apply_share_one_directory_fact() -> None:
    """Rendering and applying must never name the directory independently."""
    tasks = _tasks(_MANIFESTS)
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
        for t in _tasks(_MANIFESTS)
        if _REAL_DIR in str(t)
        and "manifests_dest_dir" not in str(t.get("ansible.builtin.set_fact", ""))
    ]
    assert not offenders, (
        f"tasks hardcode {_REAL_DIR}: {offenders}. Under k8s_dry_run these write the live "
        "staging tree, where the next `kubectl apply -f <dir>/` picks them up."
    )


def test_dry_run_renders_to_a_tempdir_and_discards_it() -> None:
    tasks = _tasks(_MANIFESTS)
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


# ------------------------------------------------------------------------------------ apply


def test_apply_is_server_dry_run_under_the_flag() -> None:
    cmd = _cmd(_named(_tasks(_MANIFESTS), "Apply manifests"))
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
        _named(_tasks(_MANIFESTS), "Apply manifests").get("changed_when", "")
    )
    assert "k8s_dry_run" in changed_when, (
        "changed_when no longer excludes dry-run. Dry-run stdout carries the same "
        "'created'/'configured' words as a real apply, so the run reports changed and the "
        "stabilisation gate soaks a workload nothing touched."
    )


# ------------------------------------------------------------- everything downstream is off


def test_mutating_downstream_tasks_are_guarded() -> None:
    """Each of these writes to the live cluster and must carry the explicit guard.

    Explicit, not inherited from the change conditions: a dry run renders into a fresh temp dir
    every time, so `manifests_render is changed` is always true.
    """
    tasks = _tasks(_MANIFESTS)
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
    tasks = _tasks(_MANIFESTS)
    for fragment in ("Find staged manifests", "Prune staged manifests"):
        assert _GUARD in _when(_named(tasks, fragment)), (
            f"{fragment!r} runs under dry-run; the prune loop would then dereference an "
            "unregistered manifests_staged and fail the play."
        )


def test_the_render_directory_fact_survives_every_tag_selection() -> None:
    """manifests_dest_dir is read by config-tagged renders AND the deploy-tagged apply.

    `[config, deploy]` looks like it covers both and does the opposite: tags union, so the task
    is skipped by `--skip-tags deploy` — the config-only form CLAUDE.md documents — and the
    renders then die on `'manifests_dest_dir' is undefined`. Measured 2026-08-16 with
    `--tags freshrss --skip-tags deploy`. Only `always` survives all three selections.
    """
    tasks = _tasks(_MANIFESTS)
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


# ------------------------------------------------------------------- the play-level refusal


def _k8s_play() -> dict:
    for play in yaml.safe_load(_DEPLOY.read_text()) or []:
        if "k8s" in str(play.get("name", "")).lower():
            return play
    raise AssertionError("deploy.yml no longer has a k8s play")


def test_deploy_refuses_an_uncovered_dry_run() -> None:
    asserts = [
        t
        for t in _k8s_play().get("pre_tasks", [])
        if "k8s_dry_run_unsupported" in str(t.get("vars", ""))
    ]
    assert asserts, (
        "deploy.yml no longer refuses a dry run naming an uncovered role. Those roles mutate "
        "outside roles/k8s/manifests, so the run half-applies: the manifest apply is suppressed "
        "while the sidecar ConfigMap / probe Job / image push fires anyway."
    )


def test_namespace_apply_is_guarded() -> None:
    task = _named(_k8s_play()["pre_tasks"], "Apply the workload namespace")
    assert _GUARD in _when(task), (
        "the namespace apply writes to the cluster on a dry run"
    )


# --------------------------------------------------------------------------------- the vars


def _all_vars() -> dict:
    return yaml.safe_load(_ALL_VARS.read_text()) or {}


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
# write to the tool's own state (crowdsec/pihole, both already dry-run-unsupported for other
# reasons — this just gives their real exec writes a matching rule too).
_EXEC_WRITE = re.compile(
    r"\b(touch|mkdir|ln -s\w*|rm|mv|cp|tee|chmod|chown|dd)\b"
    r"|cscli \S+ (create|delete|add|remove)\b"
    r"|pihole -g\b"
)

# A plain `exec` (not `-i`) that isn't already caught by _MUTATES.
_BARE_EXEC = re.compile(r"kubectl\b[^\n]*\bexec\s+(?!-i\b)\S")


def _task_chunks(task_file: Path, strip_trailing_comments: bool = False) -> list[str]:
    """Join each task's (possibly multi-line, folded-scalar) body into one string.

    A raw per-line scan can't see `kubectl … exec pod --` and the write verb it pipes to when
    they land on different physical lines of a `cmd: >-` block — which is the normal shape here.
    Splitting on `- name:` (a task boundary in every file in this tree, block-nested or not) and
    joining what follows gives each task one flat string to search, without needing a full YAML
    parse that would miss commands nested under `block:`/`loop:`.

    Whole-line comments are always dropped. `strip_trailing_comments` additionally drops a
    trailing `#…` from every line BEFORE they are joined — per line, because joining first and
    stripping after would delete the rest of the task. That direction is only safe for a check
    that must not credit a comment (the guard search below); it is deliberately off for the
    mutation search, where dropping text could only hide a write.
    """
    chunks: list[str] = []
    current: list[str] = []
    for line in task_file.read_text().splitlines():
        stripped = line.strip()
        if re.match(r"^-\s*name:", stripped):
            if current:
                chunks.append(" ".join(current))
            current = []
        if stripped.startswith("#"):
            continue
        if strip_trailing_comments:
            stripped = re.sub(r"\s#.*$", "", stripped)
        current.append(stripped)
    if current:
        chunks.append(" ".join(current))
    return chunks


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
        for task_file in sorted((role / "tasks").glob("*.yml"))
        for chunk in _task_chunks(task_file)
    )


def _bypasses_manifests(role: Path) -> bool:
    """A role that never includes roles/k8s/manifests applies its objects some other way."""
    main = role / "tasks/main.yml"
    return main.exists() and "manifests" not in main.read_text()


# Whole-identifier, not a bare substring: janitorr's unrelated `janitorr_dry_run: "{{
# janitorr_k8s_dry_run }}"` contains the literal text "k8s_dry_run" inside a longer variable
# name, which a substring check reads as a guard that isn't there. `\b` anchors this to the
# real fact names, which are underscore-joined identifiers with no boundary in the middle of
# `janitorr_k8s_dry_run` for `\b` to land on.
_GUARD_FACT = re.compile(r"\bk8s_no_mutate\b|\bk8s_dry_run\b")


_INCLUDED_FILE = re.compile(r"(?:import_tasks|include_tasks):\s*[\"']?([\w.-]+\.ya?ml)")


def _guard_covered_files(role: Path) -> set[str]:
    """Task files every one of whose tasks inherits a no-mutation guard from its caller.

    seed-volume is the shape this exists for: main.yml is a single
    `import_tasks: seed.yml` under `when: not k8s_no_mutate`, and the import propagates that
    `when` to every task in seed.yml — and on to copy.yml, which seed.yml includes. Nothing in
    either file names the guard, so a per-task rule alone would call the role unguarded and
    demand it be added to k8s_dry_run_unsupported, where it would do nothing (the refusal reads
    --tags, and seed-volume is reached as a dependency of 25 roles).

    Only main.yml is scanned for the guarded include; from there the closure is transitive and
    unconditional, because a file whose caller is guarded is guarded whatever it does next.
    """
    tasks_dir = role / "tasks"
    main = tasks_dir / "main.yml"
    if not main.is_file():
        return set()
    pending = [
        m.group(1)
        for chunk in _task_chunks(main, strip_trailing_comments=True)
        if _GUARD_FACT.search(chunk)
        for m in [_INCLUDED_FILE.search(chunk)]
        if m
    ]
    covered: set[str] = set()
    while pending:
        name = pending.pop()
        if name in covered:
            continue
        covered.add(name)
        included = tasks_dir / name
        if not included.is_file():
            continue
        for chunk in _task_chunks(included):
            m = _INCLUDED_FILE.search(chunk)
            if m:
                pending.append(m.group(1))
    return covered


def _unguarded_mutations(role: Path) -> list[str]:
    """Every task in the role that writes to the cluster with no k8s_no_mutate guard on it.

    The guard has to sit ON the mutating task (or on the guarded include that pulled its whole
    file in), not merely somewhere in the role. `_GUARD_FACT.search(task_file.read_text())` is
    what this replaces, and it matched cronjob-gate's COMMENTS alone: deleting
    `when: not (k8s_no_mutate | bool)` from its `kubectl create job` left this file green while
    `./scripts/deploy.sh --tags configarr --dry-run` fired a real gate run and reconciled the
    live *arr stack. configarr was dropped from k8s_dry_run_unsupported on the strength of that
    guard, so this is the check the removal rests on.

    Fail-closed in two directions. A trailing comment is stripped from the guard search, so a
    `# k8s_no_mutate` in prose credits nothing. And the rule is per-task: a role that guarded a
    `block:` rather than the tasks inside it would be reported here, even though Ansible would
    propagate that `when`. No role in this tree does that today; if one is written, either move
    the guard onto the tasks or make this walker read block ancestry the way
    `test_k8s_autodeploy_guard.py::_iter_task_dicts` does.
    """
    covered = _guard_covered_files(role)
    offenders = []
    for task_file in sorted((role / "tasks").glob("*.yml")):
        if task_file.name in covered:
            continue
        guarded = _task_chunks(task_file, strip_trailing_comments=True)
        for chunk, guard_text in zip(_task_chunks(task_file), guarded):
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
    loop. n8n-images is that role, and it belongs in k8s_dry_run_unsupported.
    """
    return bool(_mutates_outside_manifests(role)) and not _unguarded_mutations(role)


def test_unsupported_list_matches_the_roles_that_actually_mutate() -> None:
    derived = {
        role.name
        for role in sorted(_K8S_ROLES.iterdir())
        if role.is_dir()
        and role.name != "manifests"
        and (role / "tasks").is_dir()
        and (_mutates_outside_manifests(role) or _bypasses_manifests(role))
        and not _guarded_at_entry(role)
    }
    declared = set(_all_vars()["k8s_dry_run_unsupported"])

    missing = sorted(derived - declared)
    stale = sorted(declared - derived)
    assert not missing, (
        f"these roles mutate the cluster outside roles/k8s/manifests but are not in "
        f"k8s_dry_run_unsupported: {missing}. A dry run naming one of them half-applies. "
        "Either add them to the list, or guard their kubectl calls on k8s_dry_run."
    )
    assert not stale, (
        f"these roles are listed as dry-run-unsupported but no longer mutate outside "
        f"roles/k8s/manifests: {stale}. Drop them from the list so dry-run covers them."
    )


def _roles_included_by_other_roles() -> set[str]:
    """Roles reachable as a dependency rather than by name on the command line."""
    included: set[str] = set()
    for role in sorted(_K8S_ROLES.iterdir()):
        if not (role / "tasks").is_dir():
            continue
        for task_file in sorted((role / "tasks").glob("*.yml")):
            for hit in re.findall(r"name:\s*k8s/([a-z0-9-]+)", task_file.read_text()):
                included.add(hit)
    return included


def test_no_role_hides_a_dependency_the_tag_refusal_cannot_see() -> None:
    """The play-level refusal keys on --tags, so a dependency slips straight past it.

    This is not hypothetical: `--tags freshrss` names no unsupported role and still ran
    seed-volume, which started and removed a pod against freshrss's live Longhorn PVC. Any role
    reachable as a dependency must therefore guard itself, not rely on the refusal.
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
        "unrelated service reaches them. Listing them in k8s_dry_run_unsupported does NOT help "
        "— that check only sees --tags. Guard tasks/main.yml on k8s_no_mutate instead."
    )


def _role_with_tasks(tmp_path: Path, **files: str) -> Path:
    role = tmp_path / "widget"
    (role / "tasks").mkdir(parents=True)
    for name, body in files.items():
        (role / "tasks" / f"{name}.yml").write_text(body)
    return role


_CREATE_JOB = (
    "- name: Run a one-off gate Job\n"
    "  tags: [deploy]\n"
    "  ansible.builtin.command:\n"
    "    cmd: k3s kubectl -n ns create job widget-deploy-gate --from=cronjob/widget\n"
)


def test_a_guard_named_only_in_a_comment_does_not_excuse_a_mutating_task(
    tmp_path: Path,
) -> None:
    """The W3 defect, pinned: `_guarded_at_entry` was a raw-text search over the whole file.

    cronjob-gate's comments alone satisfied it, so deleting the `when:` from its
    `kubectl create job` left both derivation tests green while a real dry run created the Job.
    Watched failing with that `when:` deleted from the live role: both
    `test_unsupported_list_matches_the_roles_that_actually_mutate` and
    `test_no_role_hides_a_dependency_the_tag_refusal_cannot_see` name cronjob-gate.
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
    """seed-volume's shape: the guard is on the import, not on the tasks that write.

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


# ------------------------------------------------------------------------------- the wrapper


def test_wrapper_translates_dry_run_and_skips_the_lock() -> None:
    body = _DEPLOY_SH.read_text()
    assert "-e k8s_dry_run=true" in body, (
        "scripts/deploy.sh --dry-run no longer sets the var. ansible-playbook has no --dry-run "
        "of its own, so the flag would be passed through and rejected."
    )
    assert 'dry_run" == 1' in body, (
        "the wrapper takes the git-tree lock for a run that writes nothing"
    )
