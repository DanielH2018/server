"""The could-not-read exits #4444 left at 1 move to `PROBE_UNREADABLE` (75), or say why 1 (#4468).

1 is the verdict "the probe looked and saw something bad". Each path below observed nothing
about its subject, so it either exits 75 with the failure on stderr, or carries a `# DECIDED:`
marker at the code that chooses 1, saying why 1 is its verdict. A test accepts either, which is
what the issue's Verify-by asks for, and refuses the bare 1 the code exits today.

Each test drives the real code and stubs only the process boundary under it:
`subprocess.run` for kubectl, sops and curl, or a state directory in `tmp_path`.

Run: uv run pytest scripts/diagnostics/tests/test_probe_could_not_read_exits.py
"""

import inspect
import json
import os
import shutil
import subprocess
import sys
from types import SimpleNamespace

# `scripts/diagnostics` is deliberately absent from `pythonpath` in pyproject.toml, so this
# module puts its own parent directory on `sys.path` — the insert every sibling here carries.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import probe
from diagnostics.probe_lib import gitops_view, ha, health
from diagnostics.probe_lib.core import SECRETS_PATH
from lib import kubectl
from lib.exit_codes import FAILED, PROBE_UNREADABLE

DECIDED = "# DECIDED:"
NOW = 1_800_000_000.0


def _exit_code(call):
    """The code the process would exit with: a returned int, or a raised SystemExit's code.

    A fix may return 75 or raise `core.Unreadable`; both exit 75, so both count.
    """
    try:
        return call()
    except SystemExit as exc:
        if exc.code is None:
            return 0
        return exc.code if isinstance(exc.code, int) else FAILED


def _decided_at(func, anchor):
    """Whether the blank-line-delimited block of `func` holding `anchor` has a DECIDED marker.

    The block, not the whole function: `health.run_health` already carries an unrelated marker
    further down, and that one says nothing about the cluster refusal.
    """
    lines = inspect.getsource(func).splitlines()
    hits = [i for i, line in enumerate(lines) if anchor in line]
    if not hits:
        return False
    start = end = hits[0]
    while start > 0 and lines[start - 1].strip():
        start -= 1
    while end + 1 < len(lines) and lines[end + 1].strip():
        end += 1
    return any(DECIDED in line for line in lines[start : end + 1])


def _decided_in(func):
    """Whether `func`, or the comment block directly above it, carries a DECIDED marker."""
    return DECIDED in inspect.getsource(func) or DECIDED in (
        inspect.getcomments(func) or ""
    )


def test_wrong_cluster_refusal_from_a_handler_exits_unreadable(
    tmp_path, monkeypatch, capsys
):
    """`vip-placement` on a kubectl that serves stage, not the prod it asked about.

    `lib.kubectl.kubectl` refuses before it runs anything, so nothing about the VIPs was read.
    """
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n")
    monkeypatch.setenv("KUBECONFIG", str(kubeconfig))
    monkeypatch.setattr(shutil, "which", lambda name, path=None: f"/usr/bin/{name}")
    stage_nodes = json.dumps({"items": [{"metadata": {"name": "daniel-stage"}}]})
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **_: subprocess.CompletedProcess(argv, 0, stage_nodes, ""),
    )
    # The identity read is cached per process; a cached `prod` would skip the stub above.
    kubectl.forget_served_cluster()
    try:
        rc = _exit_code(lambda: probe.main(["vip-placement"]))
    finally:
        kubectl.forget_served_cluster()
    err = capsys.readouterr().err
    if _decided_at(probe._dispatch, "except WrongCluster"):
        return
    assert rc == PROBE_UNREADABLE, f"a WrongCluster refusal exited {rc}"
    assert kubectl.cluster_refusal("prod", "stage") in err


def test_health_cluster_refusal_exits_unreadable(capsys):
    """`health --cluster stage` on a kubectl that serves prod: no workload was looked at."""
    rc = _exit_code(
        lambda: health.run_health("authelia", cluster="stage", served="prod")
    )
    err = capsys.readouterr().err
    if _decided_at(health.run_health, "cluster_refusal("):
        return
    assert rc == PROBE_UNREADABLE, f"health's cluster refusal exited {rc}"
    assert kubectl.cluster_refusal("stage", "prod") in err


def test_health_unconfirmable_cluster_exits_unreadable(capsys):
    """A node read that named no known cluster (`served=None`) is a read that failed."""
    rc = _exit_code(lambda: health.run_health("authelia", cluster="prod", served=None))
    err = capsys.readouterr().err
    if _decided_at(health.run_health, "cluster_refusal("):
        return
    assert rc == PROBE_UNREADABLE, f"an unconfirmable cluster exited {rc}"
    assert kubectl.cluster_refusal("prod", None) in err


def test_ha_state_non_json_body_exits_unreadable(monkeypatch, capsys):
    """HA answered `/api/states/<id>` with an error page, so the entity was never read."""
    body = "<html><body>502 Bad Gateway</body></html>\n"

    def run(argv, **_):
        # sops answers the domain and the token; curl answers the GET with the error page.
        reply = "example.test\n" if SECRETS_PATH in argv else body
        return subprocess.CompletedProcess(argv, 0, reply, "")

    monkeypatch.setattr(subprocess, "run", run)
    rc = _exit_code(lambda: probe.main(["ha", "state", "fan.tower_fan"]))
    out, err = capsys.readouterr()
    if _decided_at(ha.run_ha, "json.JSONDecodeError"):
        return
    assert rc == PROBE_UNREADABLE, f"a non-JSON `ha state` body exited {rc}"
    assert body.strip() in err
    assert body.strip() not in out


def test_gitops_state_unreadable_marker_exits_unreadable_or_is_decided(
    tmp_path, capsys
):
    """`hold_sha` exists and cannot be opened: the hold was not read.

    A directory in the marker's place fails the read with `IsADirectoryError` for any user,
    root included, where a `chmod 000` does not.
    """
    (tmp_path / gitops_view.MARKERS["hold"]).mkdir()
    rc = _exit_code(
        lambda: gitops_view.run_gitops_state(SimpleNamespace(json=True), tmp_path, NOW)
    )
    err = capsys.readouterr().err
    if _decided_in(gitops_view.run_gitops_state):
        return
    assert rc == PROBE_UNREADABLE, f"an unreadable marker exited {rc}"
    assert gitops_view.MARKERS["hold"] in err


def test_gitops_state_absent_directory_exits_unreadable_or_is_decided(tmp_path, capsys):
    """No state directory: either the read failed, or "not the deploy host" is the verdict."""
    missing = tmp_path / "missing"
    rc = _exit_code(
        lambda: gitops_view.run_gitops_state(SimpleNamespace(json=False), missing, NOW)
    )
    err = capsys.readouterr().err
    if _decided_in(gitops_view.run_gitops_state):
        return
    assert rc == PROBE_UNREADABLE, f"an absent state directory exited {rc}"
    assert str(missing) in err
