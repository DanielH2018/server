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

The drill-proven order and the shape of each Longhorn call are pinned in
`test_volume_revert_sequence.py`; the mutation guards in `test_volume_revert_guards.py`; the
snapshot selection in `test_volume_revert_selection.py`; the `roles/k8s/manifests` include
in `test_volume_revert_include.py`. What stays here is the input check, the seam tests
against the live API server, and the checks on the validator and dry-run lists.
"""

from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess

import pytest
import yaml
from _helpers import REPO as _REPO
from _helpers import load_tasks as _tasks
from _helpers import render_expr as _render
from _volume_revert import (
    _CLAIM,
    _DEFAULTS,
    _MAIN,
    _VALIDATOR,
    _index,
    _named,
    _no_cluster_to_ask,
    _task_names,
)
from _volume_ops import assert_the_role_declares_an_autodeploy_stance


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
    assert_the_role_declares_an_autodeploy_stance(_DEFAULTS)


def test_the_validator_skips_a_role_with_no_manifests() -> None:
    """`validate_k8s_manifests.py` renders every role's templates. This role has none, so it
    must be in SKIP_ROLES or the validator fails on an absent templates directory.

    Read as parsed set literals rather than searched for as a substring: a commented-out entry
    satisfies a substring search while the validator no longer skips anything, which is the
    mutation that found this test asserting nothing on 2026-08-21. They are parsed instead of
    imported because importing the validator pulls in `kubernetes_validate` and its sys.path
    setup for a one-line fact.

    The two component sets are read rather than `SKIP_ROLES` itself, which is now their union
    and so is a BinOp that `ast.literal_eval` refuses. Same trap this test already documents in
    a different spelling: a check that reads source text breaks the moment the source gains an
    indirection.
    """
    tree = ast.parse(_VALIDATOR.read_text())
    skipped: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        names = {t.id for t in node.targets if isinstance(t, ast.Name)}
        if names & {"NO_MANIFEST_ROLES", "CALLER_RENDERED_ROLES"}:
            skipped |= set(ast.literal_eval(node.value))
    assert skipped, (
        "neither component of SKIP_ROLES parsed — the guard is asserting nothing"
    )
    assert "volume-revert" in skipped


def test_the_role_is_absent_from_the_dry_run_refusal_list() -> None:
    """`k8s_dry_run_unsupported` keys on `ansible_run_tags` and cannot see a dependency-reached
    role, so listing this one would buy nothing. It guards itself on `k8s_no_mutate` instead —
    the choice volume-claim, image-builder, cronjob-gate and volume-snapshot all make."""
    listed = yaml.safe_load(
        (_REPO / "ansible/inventory/group_vars/all.yml").read_text()
    )["k8s_dry_run_unsupported"]
    assert "volume-revert" not in listed


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
