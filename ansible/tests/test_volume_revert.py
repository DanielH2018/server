"""What `k8s/volume-revert` does in what order, and which steps must never move.

The role runs only during an incident: a gitops auto-deploy failed, the deployer rolled the
tree back, and the old manifests are about to be applied against data the failed deploy may
already have migrated. Nothing exercises this path on a good day, so a defect here stays
dormant until the worst moment. That asymmetry is why the tests below are ordering tests as
much as content tests.

The sequence is drill-proven, not chosen (measured 2026-08-21 on `speedtest-config`, Longhorn
v1.12.1). Two plausible alternatives were measured and both fail:

  * a revert with the frontend enabled returns HTTP 500 `failed to revert snapshot for volume
    ... with frontend enabled`;
  * a revert on a plainly detached volume also returns 500, because no engine is running to
    perform it.

So the volume must be attached with `disableFrontend: true` — maintenance mode — and every
step that can fail must fail BEFORE the workload is scaled to zero. A step that fails after
the scale-down leaves the service down AND unreverted, which is worse than not trying.

**These tests exercise the decisions, not the deploy.** `kubectl` in this repo authenticates as
a read-only ServiceAccount and Ansible is the only write path to the cluster, so no volume can
be scaled, attached or reverted here. Whether the Longhorn API performs the revert is
**unexercised** by this file and nothing below should be read as covering it — task 6 of the
slice drills that against a real volume.

`test_the_listing_jsonpath_parses` is the one seam test: it runs the role's own argv against
the live API server, so the synthetic listings the selection tests inject enter where the real
ones do. `test_cronjob_gate_decision.py` records the sibling case where synthetic payloads
injected downstream of a broken command left a whole branch dead while every test passed.
"""

from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from _k8s_render import rendered_docs
from ansible.plugins.filter.core import FilterModule
from ansible.plugins.test.core import TestModule as _AnsibleTests
from jinja2.nativetypes import NativeEnvironment

_REPO = Path(__file__).resolve().parents[2]
_ROLE = _REPO / "ansible/roles/k8s/volume-revert"
_CLAIM = _ROLE / "tasks/claim.yml"
_MAIN = _ROLE / "tasks/main.yml"
_DEFAULTS = _ROLE / "defaults/main.yml"
_VALIDATOR = _REPO / "scripts/validate_k8s_manifests.py"
_MANIFESTS = _REPO / "ansible/roles/k8s/manifests/tasks/main.yml"

_GUARD = "not (k8s_no_mutate | bool)"


def _tasks(path: Path) -> list[dict]:
    return yaml.safe_load(path.read_text()) or []


def _iter_task_dicts(path: Path) -> list[dict]:
    return _tasks(path)


def _task_names(path: Path) -> list[str]:
    return [str(task.get("name", "")) for task in _tasks(path)]


def _named(path: Path, fragment: str) -> dict:
    for task in _tasks(path):
        if fragment in str(task.get("name", "")):
            return task
    raise AssertionError(
        f"no task in {path.name} whose name contains {fragment!r} — the task was renamed or "
        f"removed, and these tests would otherwise silently check nothing."
    )


def _index(names: list[str], fragment: str) -> int:
    """The position of the ONE task whose name contains `fragment`.

    Unique-match, not first-match, and that is the whole point. An ordering assert built on
    first-match is satisfied by any earlier task that happens to share the substring, so
    renaming a read task to mention `disableFrontend` would make the assert below pass while
    the assert task itself sat after the revert. This repo has shipped six tests that credit
    the argument against the thing; refusing an ambiguous match is how this one avoids being
    the seventh.
    """
    hits = [i for i, name in enumerate(names) if fragment in name]
    if len(hits) != 1:
        raise AssertionError(
            f"{fragment!r} matches {len(hits)} task names in the file under test; an ordering "
            f"assert can only pin a unique task. Rename so exactly one task carries it."
        )
    return hits[0]


# kubectl's ways of saying "there is no cluster here to ask". Measured 2026-08-21: kubectl does
# NOT print the bare string "connection refused" — it prints "The connection to the server
# localhost:8080 was refused", and a cluster without Longhorn's CRDs answers "the server doesn't
# have a resource type". A guard that misses either turns "no cluster" into a red test on any
# machine that ships kubectl, GitHub's ubuntu runners included. The first four match
# `test_volume_snapshot.py`'s list.
_NO_CLUSTER = (
    "connection refused",
    "was refused",
    "i/o timeout",
    "no configuration has been provided",
    "doesn't have a resource type",
)


def _no_cluster_to_ask(stderr: str) -> bool:
    """Whether kubectl failed for want of a cluster rather than for want of a valid jsonpath.

    A rejected jsonpath never counts as unreachable, whatever else the stderr says: that is the
    one failure the seam test exists to catch, and it reads `error: error parsing jsonpath …`.
    """
    return "jsonpath" not in stderr and any(token in stderr for token in _NO_CLUSTER)


def test_the_seam_test_skips_a_missing_cluster_and_fails_a_bad_jsonpath() -> None:
    """The seam test's own guard, against stderr recorded from kubectl on 2026-08-21.

    Without this, the guard is only exercised on a machine that happens to be in the state it
    describes — which is never this one, and never CI.
    """
    unreachable = (
        "The connection to the server localhost:8080 was refused - did you specify the right "
        "host or port?",
        "error: no configuration has been provided, try setting KUBERNETES_MASTER environment "
        "variable",
        'error: the server doesn\'t have a resource type "snapshots"',
        "Unable to connect to the server: dial tcp 10.0.0.1:6443: i/o timeout",
    )
    for stderr in unreachable:
        assert _no_cluster_to_ask(stderr), stderr
    rejected = (
        'error: error parsing jsonpath {range .items[?(@.spec.volume!=""&&@.spec.volume=="x")]}'
        ", unrecognized character in action: U+0026 '&'"
    )
    assert not _no_cluster_to_ask(rejected)
    # The combination that matters most: an unreachable-looking message must not excuse a
    # jsonpath kubectl rejected.
    assert not _no_cluster_to_ask(rejected + " connection refused")


def _env() -> NativeEnvironment:
    env = NativeEnvironment()
    env.filters.update(FilterModule().filters())
    env.tests.update(_AnsibleTests().tests())
    return env


def _render(expression: str, **context):
    return _env().from_string(expression).render(**context)


# kubectl verbs that change the cluster. `get`, `describe` and friends are deliberately absent:
# a read is what `check_mode: false` exists to let run under `--check`.
_WRITE_VERBS = {
    "scale",
    "patch",
    "apply",
    "create",
    "delete",
    "replace",
    "edit",
    "annotate",
    "label",
    "cordon",
    "uncordon",
    "drain",
    "taint",
    "exec",
    "cp",
    "rollout",
}


def _role_tasks() -> list[tuple[Path, dict]]:
    """Every task in the role, both files.

    Both files, because `main.yml` is where someone writes "and bring it back up" — and a census
    that reads only `claim.yml` cannot see it. Measured 2026-08-21: an unguarded
    `--replicas=1` appended to `main.yml` left all 27 tests green.
    """
    return [(path, task) for path in (_CLAIM, _MAIN) for task in _tasks(path)]


def _mutating_tasks() -> list[tuple[Path, dict]]:
    """Tasks that change the cluster, or that wait on a change this role made.

    The waits are included deliberately: under `k8s_no_mutate` the scale-down and the attach
    never happen, so an unguarded wait polls for a transition nobody requested and burns its
    whole timeout before failing a dry run that changed nothing.

    Recognises a write by its kubectl verb rather than by the one verb this role happens to use
    today, and recognises `kubernetes.core.*` as mutating whatever the module. The previous
    version saw only `uri`, `scale` and `until`; an appended `kubectl patch pvc/...` was
    invisible to it.
    """
    out = []
    for path, task in _role_tasks():
        modules = [
            key
            for key in task
            if "." in key and not key.startswith("ansible.builtin.set")
        ]
        if any(module.startswith("kubernetes.core.") for module in modules):
            out.append((path, task))
            continue
        if "ansible.builtin.uri" in task:
            out.append((path, task))
            continue
        command = task.get("ansible.builtin.command") or task.get(
            "ansible.builtin.shell"
        )
        if not isinstance(command, dict):
            continue
        argv = [str(token) for token in command.get("argv", [])]
        if _WRITE_VERBS.intersection(argv) or "until" in task:
            out.append((path, task))
    return out


def _guard_of(task: dict) -> list[str]:
    when = task.get("when")
    conditions = when if isinstance(when, list) else [when]
    return [str(condition).strip() for condition in conditions]


def _is_guarded(task: dict) -> bool:
    return _GUARD in _guard_of(task)


_PROSE_KEYS = {"name", "msg", "fail_msg", "success_msg"}


def _strip_prose(node):
    """Drop the human-readable fields, at any depth.

    Everything left is something Ansible acts on. The prose is dropped because it legitimately
    mentions the very strings the checks below hunt for — the frontend assert's failure message
    says the workload "is at zero replicas", and an operator reading it mid-incident needs that
    sentence more than a scanner needs a simpler rule.
    """
    if isinstance(node, dict):
        return {k: _strip_prose(v) for k, v in node.items() if k not in _PROSE_KEYS}
    if isinstance(node, list):
        return [_strip_prose(item) for item in node]
    return node


def _body(task: dict) -> str:
    """The task as YAML, minus the prose fields."""
    return yaml.safe_dump(_strip_prose(task))


def _body_with_prose(task: dict) -> str:
    """The task as YAML, minus its name only.

    The register check below needs the messages: a `debug` whose `msg` templates a skipped
    task's register fails the dry run exactly like a `when` that reads one.
    """
    return yaml.safe_dump({key: value for key, value in task.items() if key != "name"})


# --------------------------------------------------------------------------------- ordering


def test_the_revert_asserts_the_frontend_is_disabled_before_reverting() -> None:
    """Measured 2026-08-21: a revert with the frontend enabled returns HTTP 500 `failed to
    revert snapshot for volume ... with frontend enabled`. The assert is the precondition, not
    a formality — without it the revert fails late, after the workload is already scaled to
    zero, leaving the service down AND unreverted."""
    tasks = _task_names(_CLAIM)
    assert _index(tasks, "disableFrontend") < _index(tasks, "Revert the volume")


def test_a_missing_snapshot_fails_rather_than_skipping() -> None:
    """A rollback that silently finds no snapshot and proceeds is the exact bug this slice
    exists to fix: the deploy would restore the old manifests against migrated data. Skipping
    is the fail-open direction and must not be reachable."""
    task = _named(_CLAIM, "Fail when no snapshot matches this deploy")
    assert "ansible.builtin.fail" in task
    assert "failed_when: false" not in yaml.safe_dump(task)
    assert "volume_revert_candidates | length == 0" in _guard_of(task)


def test_a_dry_run_reports_a_missing_snapshot_instead_of_aborting() -> None:
    """The failure above is guarded, and something must cover the other half.

    `--check` and `--dry-run` run the two reads for real, so a service that has never deployed
    legitimately has no snapshot — and an unguarded `fail` would abort the dry run rather than
    answer its question. The pair is: fail when mutating, report when not.
    """
    fail = _guard_of(_named(_CLAIM, "Fail when no snapshot matches this deploy"))
    report = _guard_of(_named(_CLAIM, "Report a dry run with nothing to revert"))
    assert _GUARD in fail
    assert "k8s_no_mutate | bool" in report
    assert _GUARD not in report
    assert "volume_revert_candidates | length == 0" in report


def test_the_role_never_scales_back_up() -> None:
    """Every one of the thirteen manifests carries an explicit `replicas: 1`, so the apply that
    follows this role restores the Deployment. Scaling back here would roll the workload twice
    and race the apply.

    Both files, and the class rather than the literal. Measured 2026-08-21: appending an
    unguarded `kubectl scale ... --replicas=1` to `main.yml` left all 27 tests green, because
    the check read `claim.yml` alone; and a scale-back can equally be written
    `--replicas={{ n }}`, `kubernetes.core.k8s_scale`, or `kubectl patch -p '{"spec":
    {"replicas":1}}'`. So: the only replica count this role may name anywhere is zero.
    """
    for _path, task in _role_tasks():
        body = _body(task)
        assert "k8s_scale" not in body, task["name"]
        for match in re.finditer(r"replicas", body):
            # `=0` and then a non-digit, not merely the two characters `=0`. Measured
            # 2026-08-21: a decoy `--replicas=01` — a scale to ONE, spelled to look like zero —
            # satisfied the two-character check and passed. Nothing else caught it either,
            # except the pinned census count, and that stops helping the moment someone
            # legitimately adds an eighth mutating task and bumps the number.
            assert re.match(r"=0(?![0-9])", body[match.end() :]), (
                f"{task['name']!r} names a replica count that is not zero: "
                f"...{body[match.start() - 20 : match.end() + 20]}... The apply that follows a "
                f"revert restores the Deployment; this role only ever scales to zero."
            )
        argv = [
            str(token)
            for token in (task.get("ansible.builtin.command") or {}).get("argv", [])
        ]
        if "scale" in argv:
            assert "to zero replicas" in task["name"], task["name"]


def test_the_snapshot_lookup_precedes_the_scale_down() -> None:
    """The frontend assert is not the only step whose lateness costs an outage. "No snapshot
    matches this deploy" is a legitimate outcome — the service's first deploy takes no snapshot
    at all — and reaching it after `--replicas=0` leaves the workload down with nothing to
    revert to. The lookup and its failure both belong upstream of the scale-down."""
    tasks = _task_names(_CLAIM)
    assert _index(tasks, "Fail when no snapshot matches this deploy") < _index(
        tasks, "to zero replicas"
    )


def test_the_whole_sequence_is_in_the_drill_proven_order() -> None:
    """Every step of claim.yml, pinned as one sequence.

    Pairwise asserts leave the pairs nobody thought of unpinned, and two of those transpositions
    are outages. Measured 2026-08-21: moving the scale-down AFTER the maintenance-mode attach
    left every other test green, and at runtime the pod still holds the volume, so the attach
    cannot give the engine a disabled frontend — service down, unreverted. Moving the detach
    BEFORE the revert is the same shape: the revert then hits a plainly detached volume and gets
    the drill-measured HTTP 500, again with the workload already at zero.

    The sequence must also be exhaustive: a task in the file and not in this list fails here,
    which is what makes someone adding a step decide where it belongs.
    """
    expected = [
        "Resolve the Longhorn volume backing",
        "Check the Longhorn volume binding",
        "Name the snapshot prefix",
        "taken by this deploy",
        "Choose the newest matching snapshot",
        "Fail when no snapshot matches this deploy",
        "Report a dry run with nothing to revert",
        "Record the snapshot to revert to",
        "to zero replicas",
        "the detach that precedes the attach",
        "in maintenance mode",
        "maintenance-mode attach of",
        "disableFrontend",
        "Revert the volume",
        "Detach the volume",
        "the detach after the revert",
    ]
    names = _task_names(_CLAIM)
    assert len(names) == len(expected), (
        f"claim.yml has {len(names)} tasks and this sequence names {len(expected)}. A step that "
        f"is not in the list is a step whose position nothing checks — add it where it belongs."
    )
    positions = [_index(names, fragment) for fragment in expected]
    assert positions == sorted(positions), (
        "claim.yml's tasks are not in the drill-proven order. Read the table in the role's "
        f"CLAUDE.md before reordering. Positions found: {dict(zip(expected, positions))}"
    )
    assert positions == list(range(len(expected)))


def test_the_api_resolve_precedes_the_first_claim() -> None:
    """`longhorn_api` is resolved once in main.yml. Resolving it inside claim.yml, or after the
    loop, would put a failure that has nothing to do with this service (no longhorn-manager on
    this node) downstream of the scale-down."""
    tasks = _task_names(_MAIN)
    assert _index(tasks, "Resolve the node-local Longhorn API") < _index(
        tasks, "Revert every volume"
    )


def test_the_api_resolve_names_the_resolve_entry_point() -> None:
    """`k8s/longhorn-api`'s `tasks/main.yml` exists only to fail loudly at a caller who forgets
    `tasks_from`. A bare include therefore aborts the play — and dropping `tasks_from` while
    keeping the include is the exact edit that looks like a simplification."""
    task = _named(_MAIN, "Resolve the node-local Longhorn API")
    include = task["ansible.builtin.include_role"]
    assert include["name"] == "k8s/longhorn-api"
    assert include["tasks_from"] == "resolve.yml"


# ------------------------------------------------------------------------- the API calls


def test_neither_attach_nor_detach_sends_an_attachment_id() -> None:
    """The two calls pair on the attachment ticket's key, and the key is the empty string.

    Read from longhorn-manager v1.12.1: `manager.Attach` stores the ticket under the
    `attachmentID` the caller sent, and `manager.Detach` does `delete(tickets, attachmentID)`
    and IGNORES `hostId` entirely. Sending an `attachmentID` on the attach alone therefore
    makes the detach delete nothing — and return HTTP 200 while doing it, leaving the volume
    attached with its frontend disabled and the workload at zero. Both calls omit it, so both
    key the ticket `""`.
    """
    for fragment in ("in maintenance mode", "Detach the volume"):
        body = _named(_CLAIM, fragment)["ansible.builtin.uri"].get("body", {})
        assert "attachmentID" not in body, (
            f"{fragment!r} sends an attachmentID; the attach and the detach must agree on the "
            f"ticket key, and only the empty key is drill-proven."
        )


def test_the_detach_does_not_pretend_hostid_matters() -> None:
    """`manager.Detach` at v1.12.1 accepts `hostId` and never reads it. Sending it documents a
    guarantee the server does not provide, and the next reader would take the detach for
    host-scoped when it is ticket-scoped."""
    body = _named(_CLAIM, "Detach the volume")["ansible.builtin.uri"].get("body", {})
    assert "hostId" not in body


def test_the_attach_requests_maintenance_mode_on_this_node() -> None:
    """`disableFrontend: true` is what makes the revert possible at all, and `hostId` is what
    keeps the attach on the node whose manager is answering."""
    body = _named(_CLAIM, "in maintenance mode")["ansible.builtin.uri"]["body"]
    assert body["disableFrontend"] is True
    assert body["hostId"] == "{{ longhorn_api_node }}"


def test_every_api_call_pins_a_single_status_code() -> None:
    """A range accepts a 2xx that did not do the work. Longhorn answers a successful action
    with 200, so 200 is what each call demands."""
    for task in _tasks(_CLAIM):
        uri = task.get("ansible.builtin.uri")
        if uri is None:
            continue
        assert uri["status_code"] == 200, task["name"]
        assert uri["url"].startswith("{{ longhorn_api }}/v1/volumes/"), task["name"]


def test_the_post_revert_detach_is_verified_by_state() -> None:
    """The detach returns 200 whether or not it removed a ticket, so its own status proves
    nothing. The wait on `state: detached` is the only thing that catches a detach that did
    not detach, and suppressing its failure would restore the silence."""
    task = _named(_CLAIM, "the detach after the revert")
    dumped = yaml.safe_dump(task)
    assert "failed_when: false" not in dumped
    assert "ignore_errors" not in dumped
    assert task["until"].strip().endswith("== 'detached'")


# ------------------------------------------------------------------- guards and mechanics


def test_every_mutation_is_guarded_on_k8s_no_mutate() -> None:
    """`k8s_no_mutate` is `ansible_check_mode or (k8s_dry_run | bool)`. Guarding on either half
    alone leaves the other half mutating a live cluster during a run that promised not to.

    WHICH TEST COVERS WHICH GUARD. Nine tasks in `claim.yml` carry the guard, and three
    mechanisms divide them — jointly exhaustive as of 2026-08-21, and nothing makes them stay
    that way:

      * seven by this census — the scale-down, the three waits, and the three API calls;
      * one by `test_nothing_unguarded_reads_a_guarded_tasks_output` — the frontend assert,
        caught through its read of `volume_revert_attached` rather than as a mutation;
      * one by two dedicated tests — `Fail when no snapshot matches this deploy`, which is a
        `fail` with no register and no kubectl verb, so BOTH generic rules are blind to it.

    A tenth guarded task is therefore not automatically covered. Work out which of the three
    would notice it, and if the answer is none, write the test that does.
    """
    mutating = _mutating_tasks()
    # An exact count, not a floor. The census recognises a write by its kubectl verb, every
    # `kubernetes.core.*` module and every polling wait — but a task shaped like none of those
    # would still be invisible, and a floor would let it arrive unguarded with this test green.
    # Pinning the number makes whoever adds a task read this comment.
    assert len(mutating) == 7, (
        f"the mutating-task census found {len(mutating)} tasks, not 7. If you added a mutation, "
        f"guard it and update this count; if the census stopped recognising one, fix "
        f"_mutating_tasks — a task it cannot see is a task this test does not check."
    )
    for _path, task in mutating:
        assert _is_guarded(task), task["name"]


def test_the_three_guard_rules_cover_every_guarded_task() -> None:
    """The arithmetic in the census docstring, made executable.

    Each guarded task must be caught by at least one of the three mechanisms. A tenth guarded
    task shaped like none of them — another `fail`, a `debug`, a `wait_for` — would otherwise
    sit there with its guard checked by nothing, which is precisely how a guard gets dropped in
    a later edit and noticed by no test.
    """
    guarded_outputs = _guarded_outputs()
    # By name, not identity: every helper re-parses the YAML, so the same task is a different
    # dict object each call.
    census = {task["name"] for _path, task in _mutating_tasks()}
    uncovered = []
    for path, task in _role_tasks():
        if path != _CLAIM or not _is_guarded(task):
            continue
        by_census = task["name"] in census
        own = set(task.get("ansible.builtin.set_fact") or {}) | {task.get("register")}
        body = _body_with_prose(task)
        by_output = any(name in body for name in guarded_outputs - own)
        by_dedicated = "Fail when no snapshot matches this deploy" in task["name"]
        if not (by_census or by_output or by_dedicated):
            uncovered.append(task["name"])
    assert not uncovered, (
        f"these guarded tasks are covered by no rule: {uncovered}. The census sees writes and "
        f"waits, the output rule sees consumers of a guarded task's register or set_fact, and "
        f"the missing-snapshot failure has two tests of its own. Yours matches none — write "
        f"the test that would notice its guard disappearing."
    )


def _guarded_outputs() -> set[str]:
    """Names a `k8s_no_mutate`-guarded task produces: its `register`, and its `set_fact` keys."""
    outputs = set()
    for _path, task in _role_tasks():
        if not _is_guarded(task):
            continue
        if "register" in task:
            outputs.add(task["register"])
        outputs.update(task.get("ansible.builtin.set_fact") or {})
    return outputs


def test_nothing_unguarded_reads_a_guarded_tasks_output() -> None:
    """A task that consumes a guarded task's output must carry the same guard.

    Under `--check` the guarded task is skipped, its output is undefined, and the consumer
    fails the dry run — a run that promised to change nothing instead changes nothing and dies.
    Measured 2026-08-21: deleting the guard from the frontend assert, whose `that` reads
    `volume_revert_attached`, left all 27 tests green, because an `assert` is not a mutation.

    Output means `register` AND `set_fact`. A guarded `set_fact` is skipped exactly like a
    guarded command, and its keys are undefined for the same reason — `volume_revert_snapshot`
    is that shape today, produced by a `set_fact` and read by the guarded revert. No guarded
    `set_fact` exists as of 2026-08-21, so this half of the rule currently names nothing; it is
    here so the next one is covered by the rule rather than by whoever writes it being careful.
    """
    guarded_outputs = _guarded_outputs()
    assert guarded_outputs, "no guarded task produces anything; this test reads nothing"
    for _path, task in _role_tasks():
        if _is_guarded(task):
            continue
        body = _body_with_prose(task)
        for name in guarded_outputs:
            assert name not in body, (
                f"{task['name']!r} reads {name!r}, which a `{_GUARD}`-guarded task produces, "
                f"but carries no such guard of its own."
            )


def test_every_read_a_later_task_depends_on_runs_under_check() -> None:
    """A command task is SKIPPED under `--check` by default, and a skipped read does not fail —
    it fails its consumer several tasks later with an undefined attribute, blamed on the wrong
    task. Both reads here feed a `when`, an assert, or the snapshot selection."""
    for fragment in ("Resolve the Longhorn volume backing", "taken by this deploy"):
        assert _named(_CLAIM, fragment)["check_mode"] is False, fragment


def test_no_command_uses_a_shlex_split_string() -> None:
    """`ansible.builtin.command` shlex-splits `cmd:`, which silently tears any argument
    containing a space in half — how slice 4's gate never ran. `argv:` invokes no shell and has
    no quoting layer to get wrong."""
    for path in (_CLAIM, _MAIN):
        for task in _tasks(path):
            command = task.get("ansible.builtin.command")
            if command is None:
                continue
            assert isinstance(command, dict), task["name"]
            assert "cmd" not in command, task["name"]
            assert command.get("argv"), task["name"]


def test_the_role_declares_its_autodeploy_stance() -> None:
    """Every role under roles/k8s/ must declare `k8s_autodeploy`; the denylist is derived from
    those declarations and a role that omits one fails four guard tests instead of this one."""
    defaults = yaml.safe_load(_DEFAULTS.read_text())
    assert defaults["k8s_autodeploy"] is False
    assert defaults["k8s_autodeploy_reason"]


def test_the_validator_skips_a_role_with_no_manifests() -> None:
    """`validate_k8s_manifests.py` renders every role's templates. This role has none, so it
    must be in SKIP_ROLES or the validator fails on an absent templates directory.

    Read as a parsed set literal rather than searched for as a substring: a commented-out entry
    satisfies a substring search while the validator no longer skips anything, which is the
    mutation that found this test asserting nothing on 2026-08-21. The set is parsed instead of
    imported because importing the validator pulls in `kubernetes_validate` and its sys.path
    setup for a one-line fact.
    """
    tree = ast.parse(_VALIDATOR.read_text())
    skip_roles = next(
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "SKIP_ROLES" for t in node.targets)
    )
    assert "volume-revert" in ast.literal_eval(skip_roles)


def test_the_role_is_absent_from_the_dry_run_refusal_list() -> None:
    """`k8s_dry_run_unsupported` keys on `ansible_run_tags` and cannot see a dependency-reached
    role, so listing this one would buy nothing. It guards itself on `k8s_no_mutate` instead —
    the choice seed-volume, image-builder, cronjob-gate and volume-snapshot all make."""
    listed = yaml.safe_load(
        (_REPO / "ansible/inventory/group_vars/all.yml").read_text()
    )["k8s_dry_run_unsupported"]
    assert "volume-revert" not in listed


# ------------------------------------------------------------------ the snapshot selection


def _selection(
    lines: list[str], sha: str = "abc12345", claim: str = "speedtest-config"
):
    """Render the role's own selection expression over a synthetic listing.

    The expression is read out of the live role by task name rather than copied here, so an
    edit to the role is what these tests see. `split`, `match` and `regex_escape` come from
    Ansible's own plugins, so they render against the code Ansible runs.
    """
    task = _named(_CLAIM, "Choose the newest matching snapshot")
    expression = task["ansible.builtin.set_fact"]["volume_revert_candidates"]
    prefix = _named(_CLAIM, "Name the snapshot prefix")["ansible.builtin.set_fact"][
        "volume_revert_prefix"
    ]
    rendered_prefix = _render(
        prefix,
        volume_revert_service="speedtest",
        volume_revert_sha=sha,
        volume_revert_claim=claim,
    )
    return _render(
        expression,
        volume_revert_existing={"stdout_lines": lines},
        volume_revert_prefix=str(rendered_prefix).strip(),
    )


_NEWEST = "2026-08-21T18:00:00Z|false|autodeploy-speedtest-abc12345-speedtest-config-20260821180000"
_OLDER = "2026-08-21T09:00:00Z|false|autodeploy-speedtest-abc12345-speedtest-config-20260821090000"


def test_the_selection_takes_the_newest_by_creation_timestamp() -> None:
    """One SHA can own several snapshots: volume-snapshot appends a per-run token, so a dirty
    tree deployed twice, or a retried deploy, leaves two CRs sharing the prefix. CR names are
    not chronologically sortable as strings, so the choice is made on `creationTimestamp`."""
    assert _selection([_OLDER, _NEWEST])[0].endswith("20260821180000")
    assert _selection([_NEWEST, _OLDER])[0].endswith("20260821180000")
    # The name and the timestamp deliberately disagree here: the newer CR carries the smaller
    # token. Sorting on the name would pick the other one, which is the whole reason the sort
    # names `creationTimestamp` — a listing where the two agree cannot tell the two sorts apart.
    misnamed = "2026-08-21T19:00:00Z|false|autodeploy-speedtest-abc12345-speedtest-config-00000000000001"
    assert _selection([_NEWEST, misnamed])[0].endswith("00000000000001")


def test_the_selection_rejects_a_markremoved_snapshot() -> None:
    """Measured 2026-08-21: a snapshot already `markRemoved` cannot be reverted to. Taking one
    would fail the revert after the scale-down — and a retention pass racing a rollback is
    exactly how the newest becomes markRemoved."""
    removed = _NEWEST.replace("|false|", "|true|")
    assert _selection([_OLDER, removed]) == [
        "autodeploy-speedtest-abc12345-speedtest-config-20260821090000"
    ]


def test_the_selection_reads_an_unpopulated_markremoved_as_live() -> None:
    """A snapshot read moments after creation can have `status.markRemoved` unwritten, which
    renders as an empty field. Not-removed is the correct read of an empty value; only an
    explicit `true` means removed, and `equalto 'false'` would drop a live snapshot."""
    assert _selection([_NEWEST.replace("|false|", "||")]) == [
        "autodeploy-speedtest-abc12345-speedtest-config-20260821180000"
    ]


def test_the_selection_ignores_another_deploys_snapshots() -> None:
    """The prefix carries the service, the deploy's SHA and the claim. Reverting to a snapshot
    from a different commit restores data this deploy never wrote, and a different claim's
    snapshot belongs to a different volume entirely."""
    other_sha = _NEWEST.replace("abc12345", "def67890")
    other_claim = _NEWEST.replace("speedtest-config", "speedtest-other")
    assert _selection([other_sha, other_claim]) == []


def test_the_prefix_ends_at_a_claim_boundary() -> None:
    """Without the trailing separator, the prefix for claim `code-server-config` also matches a
    snapshot of `code-server-configmap`. The prefix ends on the `-` that precedes the run token,
    so a claim name that merely starts with another cannot answer for it.

    The residual case the separator does NOT close — a claim named `<other-claim>-something` —
    cannot matter, because the listing this filters is already scoped to THIS claim's own
    Longhorn volume in the jsonpath. No claim's snapshot ever appears in another claim's
    listing. Measured 2026-08-21: no pair of the thirteen declared claims has either shape.
    """
    prefix = _named(_CLAIM, "Name the snapshot prefix")["ansible.builtin.set_fact"][
        "volume_revert_prefix"
    ]
    assert str(prefix).strip().endswith("-")
    longer = "2026-08-21T18:00:00Z|false|autodeploy-speedtest-abc12345-speedtest-configmap-20260821180000"
    assert _selection([longer]) == []


def test_the_prefix_uses_the_callers_sha_verbatim() -> None:
    """`git rev-parse --short=8` returns MORE than eight characters when eight are ambiguous,
    and volume-snapshot names the snapshot from that raw stdout. Truncating here would fail to
    match a nine-character name at the `-<claim>-` boundary, so the caller's string is used as
    given and only its shape is checked."""
    prefix = str(
        _named(_CLAIM, "Name the snapshot prefix")["ansible.builtin.set_fact"][
            "volume_revert_prefix"
        ]
    )
    assert "volume_revert_sha }}" in prefix or "volume_revert_sha}}" in prefix
    assert "[:8]" not in prefix and "truncate" not in prefix
    nine = "abc123456"
    line = _NEWEST.replace("abc12345-", f"{nine}-")
    assert _selection([line], sha=nine) == [
        f"autodeploy-speedtest-{nine}-speedtest-config-20260821180000"
    ]


def test_the_inputs_are_checked_before_anything_moves() -> None:
    """A missing SHA renders the prefix `autodeploy-<svc>--<claim>-`, which matches nothing,
    and the role would discover that one task after the scale-down. The assert names the
    problem while the workload is still up."""
    names = _task_names(_MAIN)
    assert _index(names, "Check that volume-revert was given") < _index(
        names, "Revert every volume"
    )
    that = _named(_MAIN, "Check that volume-revert was given")[
        "ansible.builtin.assert"
    ]["that"]
    joined = " ".join(that)
    assert "volume_revert_service" in joined
    assert "volume_revert_claims" in joined
    assert "volume_revert_sha" in joined


def _input_check(**context) -> list[bool]:
    """Render the input assert's clauses against a caller's vars, as Ansible would."""
    that = _named(_MAIN, "Check that volume-revert was given")[
        "ansible.builtin.assert"
    ]["that"]
    return [bool(_render("{{ " + clause + " }}", **context)) for clause in that]


_GOOD_INPUT = {
    "volume_revert_service": "tdarr",
    "volume_revert_claims": ["tdarr-configs", "tdarr-server"],
    "volume_revert_sha": "abc12345",
}


def test_the_input_check_accepts_a_real_call() -> None:
    """The rejections below prove nothing if the clauses reject everything."""
    assert all(_input_check(**_GOOD_INPUT))


def test_a_bare_string_is_not_a_claims_list() -> None:
    """`"tdarr-configs" | length` is 13, so a length check alone passes and Ansible's `loop:`
    then iterates the CHARACTERS — the first claim becomes a PVC named `t`. It fails safely,
    before anything moves, but on the unbound-claim assert, naming a PVC nobody wrote."""
    assert not all(
        _input_check(**{**_GOOD_INPUT, "volume_revert_claims": "tdarr-configs"})
    )


def test_the_input_check_rejects_an_empty_or_malformed_call() -> None:
    """Each of these renders a snapshot prefix that matches nothing, which the role would
    otherwise discover one task after the scale-down."""
    assert not all(_input_check(**{**_GOOD_INPUT, "volume_revert_claims": []}))
    assert not all(_input_check(**{**_GOOD_INPUT, "volume_revert_service": ""}))
    assert not all(_input_check(**{**_GOOD_INPUT, "volume_revert_sha": "master"}))


def test_the_sha_shape_is_checked_against_the_hex_it_must_be() -> None:
    """The regex is the assert's whole content, so it is worth pinning: it must accept the
    eight-or-more lowercase hex `--short=8` produces and reject a branch name, an empty string
    or a truncating typo."""
    that = _named(_MAIN, "Check that volume-revert was given")[
        "ansible.builtin.assert"
    ]["that"]
    pattern = next(
        (
            re.search(r"'(\^[^']+\$)'", str(clause))
            for clause in that
            if "^" in str(clause)
        ),
        None,
    )
    assert pattern, "no anchored regex found in the input assert"
    compiled = re.compile(pattern.group(1))
    assert compiled.match("abc12345")
    assert compiled.match("abc123456")
    assert not compiled.match("abc1234")  # seven characters is not a `--short=8` tag
    assert not compiled.match("master")
    assert not compiled.match("")
    assert not compiled.match("ABC12345")


# ---------------------------------------------------------------------------- the seam


def test_the_listing_jsonpath_parses() -> None:
    """The synthetic listings above are worth something only if the real command produces that
    shape. Run the role's own argv against the live API server.

    kubectl's jsonpath has no `&&` — verified 2026-08-21, `unrecognized character in action:
    U+0026` — which is why the volume filter is one comparison and markRemoved is filtered in
    Jinja. This test is what catches someone folding them back together.
    """
    if shutil.which("kubectl") is None:
        pytest.skip("no kubectl on PATH")
    argv = _named(_CLAIM, "taken by this deploy")["ansible.builtin.command"]["argv"]
    rendered = [
        str(_render(str(token), volume_revert_volume="pvc-does-not-exist"))
        for token in argv
    ]
    # Drop the `k3s` wrapper: the tests run as an unprivileged user against the read-only
    # kubeconfig, and `k3s kubectl` needs root here.
    assert rendered[0] == "k3s"
    result = subprocess.run(
        rendered[1:], capture_output=True, text=True, timeout=60, check=False
    )
    if _no_cluster_to_ask(result.stderr):
        pytest.skip("no reachable cluster")
    assert result.returncode == 0, (
        f"kubectl rejected the listing jsonpath: {result.stderr.strip()}"
    )
    # A volume that does not exist matches nothing, so an empty answer is the correct one —
    # what is under test is that kubectl ACCEPTED the jsonpath rather than rejecting it.
    assert result.stdout.strip() == ""


def test_the_revert_body_matches_the_servers_own_schema() -> None:
    """The action name and its input field are read from the manifest, not from memory:
    `snapshotRevert` takes a `snapshotInput`, whose only relevant field is `name`. A typo in
    either would surface as a 404 or a no-op revert during an incident."""
    task = _named(_CLAIM, "Revert the volume")["ansible.builtin.uri"]
    assert task["url"].endswith("?action=snapshotRevert")
    assert set(task["body"]) == {"name"}
    assert task["body"]["name"] == "{{ volume_revert_snapshot }}"
    assert task["body_format"] == "json"
    assert json.dumps(task["body"])  # the body must be JSON-serialisable as written


# ------------------------------------------------- the invariant that makes the design safe


def _snapshot_roles() -> dict[str, list[str]]:
    """Every role that opts into a pre-deploy snapshot, and the claims it declares."""
    roles = {}
    for defaults in sorted((_REPO / "ansible/roles/k8s").glob("*/defaults/main.yml")):
        declared = (yaml.safe_load(defaults.read_text()) or {}).get(
            "k8s_autodeploy_snapshot_pvcs"
        )
        if declared:
            roles[defaults.parents[1].name] = declared
    return roles


def test_every_snapshot_role_restores_its_own_replicas() -> None:
    """`volume-revert` scales to zero and never scales back. That is only correct because the
    apply which follows restores the workload, and it restores it only if the role renders a
    Deployment **named for the service** with an **explicit replica count**.

    Nothing else in the repo enforces that. A role that opts into a snapshot but runs a
    StatefulSet, names its workload something other than the service, or omits `replicas:` and
    inherits the API server's default would leave its service at zero replicas after a
    rollback — the outage the rollback was meant to end. This is the executable version of a
    sentence that otherwise lives only in a comment.
    """
    roles = _snapshot_roles()
    # A floor, and only as a sanity check on the reader itself: the guard below is the loop,
    # which covers however many roles opt in. Thirteen opted in as of 2026-08-21.
    assert len(roles) >= 13, (
        f"only found {len(roles)} snapshot roles; the reader is broken"
    )

    deployments: dict[str, list[dict]] = {}
    for role, _tpl, doc in rendered_docs():
        if role in roles and doc.get("kind") == "Deployment":
            deployments.setdefault(role, []).append(doc)

    for role in sorted(roles):
        named = [
            doc
            for doc in deployments.get(role, [])
            if doc.get("metadata", {}).get("name") == role
        ]
        assert named, (
            f"{role} declares k8s_autodeploy_snapshot_pvcs but renders no Deployment named "
            f"{role!r}. k8s/volume-revert scales `deployment/{role}` to zero and does not scale "
            f"it back, so this service would stay down after a rollback."
        )
        for doc in named:
            replicas = doc.get("spec", {}).get("replicas")
            assert isinstance(replicas, int) and replicas >= 1, (
                f"{role}'s Deployment has replicas={replicas!r}. It must be an explicit count "
                f"of at least one: the apply after a revert is what brings the workload back."
            )


# ------------------------------------------------------------------- the manifests call site


def test_the_revert_runs_after_the_snapshot_and_before_the_apply() -> None:
    """Order is the whole contract. A revert AFTER the apply restores the old data and then the
    new pod migrates it again. A revert BEFORE the snapshot discards the recovery point for the
    state being replaced. The three-way chain pins both adjacent pairs the insertion touches, so
    moving the revert either direction fails one of the two comparisons."""
    names = _task_names(_MANIFESTS)
    assert (
        _index(names, "Snapshot the stateful volumes")
        < _index(names, "Revert the stateful volumes")
        < _index(names, "Apply manifests")
    )


def test_the_revert_is_inert_without_the_restore_var() -> None:
    """~50 roles call k8s/manifests. On every ordinary deploy — which is all of them but a
    failed auto-deploy's second attempt — this include must not run.

    Anchored on the full comparison, not on the variable's name appearing somewhere in the
    clause: `test_the_role_never_scales_back_up` found `--replicas=01` passing a two-character
    substring check elsewhere in this file, and `| length >= 0` is the same shape of hole here
    — it contains the variable name, reads as a guard, and is true for every deploy.
    """
    when = _named(_MANIFESTS, "Revert the stateful volumes")["when"]
    assert any(
        re.search(r"k8s_restore_snapshot_sha.*\)\s*\|\s*length\s*>\s*0", c)
        for c in when
    ), when
    assert _GUARD in when


def test_a_role_with_no_declared_claims_never_reverts() -> None:
    """The extra-var is global to the playbook run, so it is set for every service in the
    failing batch — including stateless ones with no snapshots at all. Those must skip, not
    fail looking for a snapshot that was never taken.

    Anchored on the full comparison for the same reason as the sha clause above: the variable's
    name appearing in the clause is not evidence the clause excludes an empty list.
    """
    when = _named(_MANIFESTS, "Revert the stateful volumes")["when"]
    assert any(
        re.search(r"k8s_autodeploy_snapshot_pvcs.*\)\s*\|\s*length\s*\)\s*>\s*0", c)
        for c in when
    ), when


def test_the_revert_include_passes_the_roles_own_interface() -> None:
    """The three vars k8s/volume-revert's own assert task requires, named exactly as that role
    reads them — a typo here reads as a missing var at runtime, after the workload is already
    scaled to zero on a real incident."""
    task = _named(_MANIFESTS, "Revert the stateful volumes")
    include = task["ansible.builtin.include_role"]
    assert include["name"] == "k8s/volume-revert"
    call_vars = task["vars"]
    assert call_vars["volume_revert_claims"] == "{{ k8s_autodeploy_snapshot_pvcs }}"
    assert call_vars["volume_revert_service"] == "{{ manifests_service }}"
    assert call_vars["volume_revert_sha"] == "{{ k8s_restore_snapshot_sha }}"


def test_the_revert_does_not_swallow_a_failed_claim() -> None:
    """The call site must not blunt the role's own fail-fast behaviour. `k8s/volume-revert`
    already stops the play on a failed claim; adding `ignore_errors` or `failed_when: false`
    here would turn that into a partial revert nothing then flags."""
    task = _named(_MANIFESTS, "Revert the stateful volumes")
    assert "ignore_errors" not in task
    assert "failed_when" not in task
