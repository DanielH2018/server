"""What `k8s/volume-snapshot` retains, and what it refuses to delete.

The role's whole value is a negative claim — "if this deploy eats the data, there is a way
back" — and every failure mode is silent. A snapshot that was never taken, a prune that deleted
the newest, a listing that read nothing and therefore pruned nothing: all three leave a green
deploy and an operator who finds out during an incident.

So these tests pin the two decisions that can be wrong without anyone noticing:

  * **the retention window** — newest-first, `markRemoved` CRs excluded from the count, and the
    newest never a candidate whatever `volume_snapshot_retain` says;
  * **the name/prefix coupling** — the prune selects on `volume_snapshot_prefix`, so a snapshot
    named without that prefix would be invisible to its own retention pass and accumulate
    forever.

**These tests exercise the decisions, not the deploy.** `kubectl` in this repo authenticates as
a read-only ServiceAccount and Ansible is the only write path to the cluster, so no snapshot can
be created here. Whether a hand-applied Snapshot CR with `createSnapshot: true` actually produces
a snapshot is **unexercised** and nothing below should be read as covering it.

**Where the synthetic payload enters.** `test_the_listing_jsonpath_parses` runs the role's own
argv against the live API server, so the `stdout_lines` the retention tests inject enter at the
seam the real ones do. That test is not decoration: `test_cronjob_gate_decision.py` records the
sibling case where synthetic payloads injected downstream of a broken `cmd:` string left an
entire branch dead while every test passed.

`split` and `match` are pulled from Ansible's own plugins rather than reimplemented, so the
expressions render against the same code Ansible runs. `max` and `equalto` are Jinja2 builtins
and are already present. The remaining divergence is `jinja2.nativetypes` returning real Python
objects where Ansible renders "True"/"False" strings — which the role's `| int` and `| bool`
coercions collapse identically.

The retention window and the name/prefix coupling are pinned in
`test_volume_snapshot_retention.py`; the maintenance-attach path for a detached volume in
`test_volume_snapshot_maintenance.py`. What stays here is deploy hygiene — every mutation
guarded, argv-form kubectl, the `roles/k8s/manifests` include — and the two seam tests
against the live API server that the retention tests inject downstream of.
"""

from __future__ import annotations

import re
import shutil
import subprocess

import pytest
import yaml
from _helpers import load_tasks as _tasks
from _helpers import render_expr as _render
from _volume_snapshot import _CLAIM, _DEFAULTS, _GUARD, _MAIN, _MANIFESTS, _ROLE, _named
from _volume_ops import assert_the_role_declares_an_autodeploy_stance


#
# 7a skipped a detached volume outright, because Longhorn needs a running engine to snapshot
# it. 7b reuses k8s/longhorn-api and the maintenance-mode attach k8s/volume-revert proved: a
# detached claim gets attached with disableFrontend, snapshotted, and detached again, and only
# an attach that itself fails still falls through to the loud "UNPROTECTED" warning.


def test_the_prune_loop_slices_cleanly_at_the_defaulted_floor_values() -> None:
    """Pins the slice syntax against the values `default([])`/`default(1)` produce for a claim
    skipped as detached, where `volume_snapshot_live`/`volume_snapshot_keep` are never set (the
    'Choose which older snapshots' task that sets them shares this task's guard). Ansible's own
    `default` filter — which only coalesces its own Undefined marker, not a bare Jinja one — is
    not exercised through this harness's plain NativeEnvironment, so this pins the syntax it
    defaults into rather than the coalescing itself."""
    loop_expr = _named(_CLAIM, "Prune snapshots beyond the retention window")["loop"]
    assert _render(loop_expr, volume_snapshot_live=[], volume_snapshot_keep=1) == []


def test_every_mutating_task_is_guarded() -> None:
    """`test_k8s_dry_run.py` derives this cluster-wide; pinned here too because this role's
    mutations are a `delete` against Longhorn snapshots — the one thing a dry run must never do.
    Matched on the MODULE'S ARGUMENTS, not on the whole task. Stringifying the task also sweeps
    in its name and messages, so a read-only task that merely mentions "delete" in prose reads
    as a mutation — and the only way to quiet it is to add a `k8s_no_mutate` guard to a task
    that does not mutate, which silently removes that check from every dry run. Narrowing to the
    arguments loses no real mutation: they are all `command`/`shell` running kubectl.
    """
    for task in _tasks(_CLAIM):
        args = "".join(
            str(task.get(module, ""))
            for module in ("ansible.builtin.command", "ansible.builtin.shell")
        )
        if "apply" in args or "delete" in args:
            assert _GUARD in str(task.get("when", "")), (
                f"task {task.get('name')!r} mutates the cluster without the "
                f"k8s_no_mutate guard"
            )


def test_the_prune_is_guarded_as_a_whole_not_only_at_the_delete() -> None:
    """Under a no-mutation run the snapshot was never taken, so a window computed from the live
    list is short by one — and the delete it feeds would remove a real snapshot."""
    for fragment in (
        "List this service's live snapshots",
        "Choose which older snapshots to prune",
        "Prune snapshots beyond the retention window",
    ):
        assert _GUARD in str(_named(_CLAIM, fragment).get("when", "")), fragment


def test_the_delete_never_waits_on_the_finalizer() -> None:
    """A Snapshot CR's `longhorn.io` finalizer makes a default `kubectl delete` block until the
    volume coalesces the data — measured hanging a drill run for twelve minutes on 2026-08-21."""
    argv = _named(_CLAIM, "Prune snapshots beyond the retention window")[
        "ansible.builtin.command"
    ]["argv"]
    assert "--wait=false" in argv
    assert "--ignore-not-found" in argv


def test_every_kubectl_call_uses_argv() -> None:
    """`ansible.builtin.command` shlex-splits a `cmd:` string, so a jsonpath containing a space
    is torn in two — the slice-4 defect that made a whole branch dead code for a round. The
    listing's `{range .items[?(...)]}` is exactly that shape."""
    for path in (_MAIN, _CLAIM):
        for task in _tasks(path):
            command = task.get("ansible.builtin.command")
            if command is None:
                continue
            assert "argv" in command, (
                f"{path.name}: task {task.get('name')!r} uses `cmd:`; use argv so no shell-like "
                f"split can tear a jsonpath apart"
            )


def test_the_deploy_tag_uses_chdir_not_git_dash_c() -> None:
    """`git -C` does not override GIT_DIR, and a stray GIT_DIR has already made a check in this
    repo operate on the real repository instead of its fixture."""
    command = _named(_MAIN, "Resolve the deploy tag")["ansible.builtin.command"]
    assert "chdir" in command
    assert "-C" not in command["argv"]
    assert not _named(_MAIN, "Resolve the deploy tag").get("become"), (
        "git run as root refuses a checkout it considers to have dubious ownership"
    )


def test_the_reads_every_later_task_depends_on_survive_check_mode() -> None:
    """A `command` task is skipped under --check by default, and a skipped read does not fail —
    it fails its consumer several tasks later with an undefined attribute. That class cost nine
    roles a fix already."""
    for path, fragment in (
        (_MAIN, "Resolve the deploy tag"),
        (_CLAIM, "Resolve the Longhorn volume backing"),
    ):
        assert _named(path, fragment).get("check_mode") is False, fragment


def test_the_role_declares_an_autodeploy_stance() -> None:
    """Every role under roles/k8s/ must, or `k8s_autodeploy_denylist` refuses to render."""
    assert_the_role_declares_an_autodeploy_stance(_DEFAULTS)


#
# This role's whole value is a snapshot taken BEFORE the apply that can destroy what it protects.
# A snapshot moved after the apply would still create a CR, still pass readiness, still prune,
# and every test above would keep passing — "wrong without anyone noticing" is exactly the shape
# Task 1's `test_the_snapshot_name_starts_with_the_prefix_the_prune_selects_on` was written to
# catch for the name/prefix coupling, and this is the same trap for the include's position.


def _manifests_tasks() -> list[dict]:
    return yaml.safe_load(_MANIFESTS.read_text()) or []


def _manifests_index(fragment: str) -> int:
    for i, task in enumerate(_manifests_tasks()):
        if fragment in str(task.get("name", "")):
            return i
    raise AssertionError(f"no task in manifests/tasks/main.yml named {fragment!r}")


def test_the_snapshot_include_runs_before_the_apply() -> None:
    assert _manifests_index("Snapshot the stateful volumes") < _manifests_index(
        "Apply manifests"
    )


def test_the_snapshot_include_is_inert_for_a_role_that_never_opts_in() -> None:
    """`k8s_autodeploy_snapshot_pvcs` is what makes the include a no-op for the ~50 services
    that do not declare it — this is the actual guarantee, not the `grep` that finds zero
    declarations today. Task 3 adding declarations must not be able to remove this gate.

    This is a TEXT MATCH against the `when:` condition, not an execution — it proves the guard
    expression is present, not that Ansible actually skips the include at runtime. The runtime
    guarantee (a faithful toy play: roleA with the var set fires, roleB without it is skipped)
    was verified by executing that play, not by this assertion. Treat this as a regression guard
    against the condition being edited away, not as proof of the behaviour by itself.
    """
    task = _manifests_tasks()[_manifests_index("Snapshot the stateful volumes")]
    when = str(task.get("when", ""))
    assert _GUARD in when
    assert "k8s_autodeploy_snapshot_pvcs | default([])" in when


def test_the_snapshot_include_calls_the_right_role_with_the_right_vars() -> None:
    task = _manifests_tasks()[_manifests_index("Snapshot the stateful volumes")]
    include = task["ansible.builtin.include_role"]
    assert include["name"] == "k8s/volume-snapshot"
    call_vars = task["vars"]
    assert call_vars["volume_snapshot_claims"] == "{{ k8s_autodeploy_snapshot_pvcs }}"
    assert "manifests_service" in call_vars["volume_snapshot_service"]


@pytest.mark.skipif(shutil.which("kubectl") is None, reason="no kubectl on this host")
def test_the_listing_jsonpath_parses() -> None:
    """The synthetic listings above are only worth something if the real command produces that
    shape. Run the role's own argv against the live API server.

    This is the seam test `test_cronjob_gate_decision.py` learned to write the hard way: a
    jsonpath kubectl rejects returns rc=1 and an empty read, every retention test above still
    passes, and the prune silently never deletes anything.

    kubectl's jsonpath has no `&&` — verified 2026-08-21, `unrecognized character in action:
    U+0026` — which is why the volume filter is one comparison and markRemoved is filtered in
    Jinja. This test is what catches someone folding them back together.
    """
    argv = _named(_CLAIM, "List this service's live snapshots")[
        "ansible.builtin.command"
    ]["argv"]
    rendered = [
        str(_render(token, volume_snapshot_volume="pvc-does-not-exist"))
        for token in argv
    ]
    # Drop the `k3s` wrapper: the tests run as an unprivileged user against the read-only
    # kubeconfig, and `k3s kubectl` needs root here.
    assert rendered[0] == "k3s"
    result = subprocess.run(
        rendered[1:], capture_output=True, text=True, timeout=30, check=False
    )
    unreachable_tokens = (
        "connection refused",
        "was refused",
        "i/o timeout",
        "no configuration has been provided",
    )
    if any(token in result.stderr for token in unreachable_tokens):
        pytest.skip("no reachable cluster")
    assert result.returncode == 0, (
        f"kubectl rejected the listing jsonpath: {result.stderr.strip()}"
    )
    # A filter matching nothing returns empty, which is the correct answer for a volume that
    # does not exist — and proves the expression parsed rather than erroring.
    assert result.stdout.strip() == ""


@pytest.mark.skipif(shutil.which("kubectl") is None, reason="no kubectl on this host")
def test_the_listing_fields_exist_on_a_real_snapshot() -> None:
    """The retention decision reads creationTimestamp, markRemoved and name.

    A field Longhorn renames would make every line unparseable and every prune a no-op, with nothing
    failing.
    """
    result = subprocess.run(
        [
            "kubectl",
            "-n",
            "longhorn-system",
            "get",
            "snapshots.longhorn.io",
            "-o",
            'jsonpath={range .items[*]}{.metadata.creationTimestamp}{"|"}'
            '{.status.markRemoved}{"|"}{.metadata.name}{"\\n"}{end}',
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        pytest.skip("no reachable cluster, or no Snapshot CRs to read")
    for line in result.stdout.strip().splitlines():
        created, removed, name = line.split("|")
        assert created.endswith("Z")
        assert removed in ("true", "false"), (
            f"markRemoved read as {removed!r}; the retention filter treats anything other than "
            f"the literal 'true' as not-removed, so this is unexpected regardless — a renamed "
            f"field reads as empty (not-removed, harmless) but any OTHER unexpected value here "
            f"would silently retain a snapshot that should have dropped out of the window"
        )
        assert name


def test_both_prefix_matchers_regex_escape_their_prefix():
    """`match` is a regex test, so a literal prefix must be escaped before it is used as one.

    2026-08-22 review. k8s/volume-revert escaped its prefix and said why in-line; the prune here
    did not, and the prune's result feeds a `kubectl delete`. Unreachable today — both the service
    and the claim are DNS-1123 labels, which carry no regex metacharacters — but the two files
    make the SAME decision and only one of them encoded it, which is the shape that becomes true
    after one rename.

    Reverting either escape fails this test: the assertion is on the filter chain in the role
    source, so an unescaped `selectattr(..., 'match', <prefix>)` no longer matches.
    """
    revert_claim = (_ROLE.parent / "volume-revert/tasks/claim.yml").read_text()
    for label, text in (
        ("volume-snapshot", _CLAIM.read_text()),
        ("volume-revert", revert_claim),
    ):
        matchers = re.findall(r"selectattr\(\s*'2',\s*'match',([^)]*)\)", text)
        assert matchers, (
            f"{label}: no `selectattr('2', 'match', ...)` prefix filter found"
        )
        for expr in matchers:
            assert "regex_escape" in expr, (
                f"{label}: `selectattr('2', 'match', {expr.strip()})` uses an UNESCAPED prefix. "
                f"`match` is a regex test, not a literal-prefix test, so a metacharacter in the "
                f"service or claim name would widen or narrow the set — and on the snapshot side "
                f"that set feeds a `kubectl delete`."
            )
