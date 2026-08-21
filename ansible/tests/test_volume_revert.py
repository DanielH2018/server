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
from ansible.plugins.filter.core import FilterModule
from ansible.plugins.test.core import TestModule as _AnsibleTests
from jinja2.nativetypes import NativeEnvironment

_REPO = Path(__file__).resolve().parents[2]
_ROLE = _REPO / "ansible/roles/k8s/volume-revert"
_CLAIM = _ROLE / "tasks/claim.yml"
_MAIN = _ROLE / "tasks/main.yml"
_DEFAULTS = _ROLE / "defaults/main.yml"
_VALIDATOR = _REPO / "scripts/validate_k8s_manifests.py"

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


def _env() -> NativeEnvironment:
    env = NativeEnvironment()
    env.filters.update(FilterModule().filters())
    env.tests.update(_AnsibleTests().tests())
    return env


def _render(expression: str, **context):
    return _env().from_string(expression).render(**context)


def _mutating_tasks(path: Path) -> list[dict]:
    """Tasks that change the cluster or wait on a change this role made.

    The waits are included deliberately: under `k8s_no_mutate` the scale-down and the attach
    never happen, so an unguarded wait polls for a transition nobody requested and burns its
    whole timeout before failing a dry run that changed nothing.
    """
    out = []
    for task in _tasks(path):
        if "ansible.builtin.uri" in task:
            out.append(task)
            continue
        command = task.get("ansible.builtin.command")
        if not isinstance(command, dict):
            continue
        argv = [str(token) for token in command.get("argv", [])]
        if "scale" in argv or "until" in task:
            out.append(task)
    return out


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


def test_the_role_never_scales_back_up() -> None:
    """Every one of the thirteen manifests carries an explicit `replicas: 1`, so the apply that
    follows this role restores the Deployment. Scaling back here would roll the workload twice
    and race the apply."""
    for task in _iter_task_dicts(_CLAIM):
        argv = task.get("ansible.builtin.command", {}).get("argv", [])
        joined = " ".join(str(t) for t in argv)
        assert "--replicas=1" not in joined


def test_the_snapshot_lookup_precedes_the_scale_down() -> None:
    """The frontend assert is not the only step whose lateness costs an outage. "No snapshot
    matches this deploy" is a legitimate outcome — the service's first deploy takes no snapshot
    at all — and reaching it after `--replicas=0` leaves the workload down with nothing to
    revert to. The lookup and its failure both belong upstream of the scale-down."""
    tasks = _task_names(_CLAIM)
    assert _index(tasks, "Fail when no snapshot matches this deploy") < _index(
        tasks, "to zero replicas"
    )


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
    alone leaves the other half mutating a live cluster during a run that promised not to."""
    mutating = _mutating_tasks(_CLAIM)
    # An exact count, not a floor. The census is a heuristic — every `uri`, plus a command that
    # scales or polls — and it is blind to a future `kubectl patch`, `apply` or `delete` that
    # neither scales nor waits. A floor would let such a task arrive unguarded with this test
    # still green; pinning the number makes whoever adds one read this comment.
    assert len(mutating) == 7, (
        f"the mutating-task census found {len(mutating)} tasks, not 7. If you added a mutation, "
        f"guard it and update this count; if the census stopped recognising one, fix "
        f"_mutating_tasks — a task it cannot see is a task this test does not check."
    )
    for task in mutating:
        when = task.get("when")
        conditions = when if isinstance(when, list) else [when]
        assert _GUARD in [str(c).strip() for c in conditions], task["name"]


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
    # Skip on an unreachable cluster, fail on anything else. kubectl does NOT print the bare
    # string "connection refused" — it prints "The connection to the server localhost:8080 was
    # refused", so a naive substring guard turns "no cluster" into a red test on any machine
    # that ships kubectl, GitHub's ubuntu runners included. The token list matches
    # `test_volume_snapshot.py`'s.
    #
    # A jsonpath kubectl rejects never skips, whatever else the stderr says: that is the one
    # failure this test exists to catch, and it reads `error: error parsing jsonpath …`.
    unreachable_tokens = (
        "connection refused",
        "was refused",
        "i/o timeout",
        "no configuration has been provided",
    )
    if "jsonpath" not in result.stderr and any(
        token in result.stderr for token in unreachable_tokens
    ):
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
