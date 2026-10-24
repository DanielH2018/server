"""`probe.py health <tag>` run from a checkout that predates the tag's k8s role (#5909).

A worktree six hours behind master had no `roles/k8s/metallb/`, so the probe guessed a workload
named `metallb` in the default namespace and failed a healthy MetalLB. The role existed on
`origin/master`, which is what lets the probe tell "this checkout is stale" from "not a k8s tag".

Run: uv run pytest scripts/diagnostics/tests/test_probe_health_checkout.py
"""

import json
import subprocess
from pathlib import Path

from deploy_tools.deploy_lib import detach_notify
from diagnostics.probe_lib import health, health_alerts, health_checkout
from lib import kubectl
from lib.exit_codes import PROBE_UNREADABLE
from lib.git_testing import commit, git, init_repo, scrub_process_git_env


def test_a_role_only_on_origin_is_unreadable_not_a_failure(tmp_path, capsys):
    """Exit 75 before any kubectl read, with a line the deploy notifier cannot read as a skip."""
    code = health.run_health(
        "metallb",
        cluster="prod",
        served="prod",
        roles_dir=tmp_path,
        on_origin=lambda role: role == "metallb",
    )
    captured = capsys.readouterr()
    assert code == PROBE_UNREADABLE
    assert "exists on origin/master but not in this checkout" in captured.err
    assert "no Deployment or DaemonSet" not in captured.out + captured.err
    assert not [m for m in detach_notify.NOT_APPLICABLE_MARKERS if m in captured.err]


def test_a_tag_absent_from_origin_too_keeps_the_guess_path(tmp_path):
    """A regression guard: a Pi tag such as `glances` must still reach `--docker`'s fallback."""
    assert (
        health_checkout.stale_checkout_reason("glances", tmp_path, lambda _r: False)
        is None
    )


def test_a_role_present_here_is_never_called_stale(tmp_path):
    (tmp_path / "metallb").mkdir()
    assert (
        health_checkout.stale_checkout_reason("metallb", tmp_path, lambda _r: True)
        is None
    )


def test_role_on_origin_reads_the_origin_master_tree(tmp_path):
    repo = init_repo(tmp_path / "repo")
    roles_dir = repo / "ansible" / "roles" / "k8s"
    assert not health_checkout.role_on_origin("metallb", roles_dir, repo)  # no ref yet
    sha = commit(
        repo, "add role", **{"ansible/roles/k8s/metallb/tasks/main.yml": "---\n"}
    )
    git(repo, "update-ref", "refs/remotes/origin/master", sha)
    assert health_checkout.role_on_origin("metallb", roles_dir, repo)
    assert not health_checkout.role_on_origin("glances", roles_dir, repo)
    # A file at the path is not a role directory.
    assert not health_checkout.role_on_origin("metallb/tasks/main.yml", roles_dir, repo)


def _behind_origin(tmp_path, role="metallb"):
    """A checkout holding `role` at commit A, whose `origin/master` changed the role at B (#5912).

    Returns `(repo, roles_dir, a, b)` with HEAD detached at A, as a long-lived orchestrator
    worktree is locked at an old master. The role directory is present here, so a check that
    reads only whether it exists cannot see that origin/master has moved it.
    """
    repo = init_repo(tmp_path / "repo")
    prefix = f"ansible/roles/k8s/{role}"
    a = commit(repo, "add role", **{f"{prefix}/tasks/main.yml": "---\n"})
    b = commit(
        repo,
        "change role",
        **{f"{prefix}/files/upstream/app.yaml": _deployment("controller")},
    )
    git(repo, "update-ref", "refs/remotes/origin/master", b)
    git(repo, "checkout", "-q", "--detach", a)
    return repo, repo / "ansible" / "roles" / "k8s", a, b


def _deployment(name, namespace="metallb-system"):
    return (
        "apiVersion: apps/v1\nkind: Deployment\n"
        f"metadata:\n  name: {name}\n  namespace: {namespace}\n"
    )


def test_a_role_origin_master_changed_after_this_checkout_is_stale(
    tmp_path, monkeypatch
):
    """A checkout behind origin/master on the role's files cannot judge it (#5912).

    The orchestrator worktree locked at `77c560a64` ran `probe.py health metallb` after
    `53b77befc` merged. A role that exists here but changed on origin/master since is the same
    exposure one step later: the gate reads the old role definition. The git read must find
    the repository from `roles_dir`, which is how a test hands it a fixture tree.
    """
    scrub_process_git_env(monkeypatch)
    repo, roles_dir, _a, b = _behind_origin(tmp_path)
    reason = health_checkout.stale_checkout_reason(
        "metallb", roles_dir, lambda _r: True
    )
    assert reason is not None
    assert "origin/master" in reason

    # At the merged SHA, or past it with the checkout's own commit, the checkout can judge.
    git(repo, "checkout", "-q", "--detach", b)
    assert (
        health_checkout.stale_checkout_reason("metallb", roles_dir, lambda _r: True)
        is None
    )
    commit(repo, "local edit", **{"ansible/roles/k8s/metallb/tasks/main.yml": "--- \n"})
    assert (
        health_checkout.stale_checkout_reason("metallb", roles_dir, lambda _r: True)
        is None
    )


def _healthy_cluster():
    """kubectl that serves the prod nodes and every Deployment Ready with one Ready pod."""
    nodes = {
        "items": [{"metadata": {"name": n}} for n in ("daniel-box", "daniel-server")]
    }
    deploy = {
        "metadata": {"generation": 1},
        "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "controller"}}},
        "status": {
            "observedGeneration": 1,
            "updatedReplicas": 1,
            "readyReplicas": 1,
            "availableReplicas": 1,
        },
    }
    ready = {
        "type": "Ready",
        "status": "True",
        "lastTransitionTime": "2026-10-22T00:00:00Z",
    }
    pods = {
        "items": [
            {
                "metadata": {"name": "p"},
                "status": {
                    "conditions": [ready],
                    "containerStatuses": [{"name": "app", "restartCount": 0}],
                },
            }
        ]
    }

    def run(argv, timeout):
        if argv[-4:] == ["get", "nodes", "-o", "json"]:
            body = nodes
        elif "pods" in argv:
            body = pods
        elif "deploy" in argv or "deployment" in argv:
            body = deploy
        else:
            return subprocess.CompletedProcess(
                argv, 1, "", "Error from server (NotFound): x"
            )
        return subprocess.CompletedProcess(argv, 0, json.dumps(body), "")

    return kubectl.Tools(
        run=run,
        find_tool=lambda name: "/usr/local/bin/kubectl",
        find_kubeconfig=lambda: Path("/tmp/kubeconfig"),
    )


def _quiet_prom():
    """An alert plane with nothing firing, whose clock moves only when the gate sleeps.

    The blackbox job is loaded and probes no route of this fixture role, which the gate
    notes and passes.
    """
    now = [1_792_800_000.0]

    def sleep(seconds):
        now[0] += seconds

    def query(promql):
        if promql == f'up{{job="{health_alerts.PROBE_JOB}"}}':
            return [
                {"metric": {"job": health_alerts.PROBE_JOB}, "value": [now[0], "1"]}
            ]
        return []

    return health_alerts.PromTools(query=query, sleep=sleep, clock=lambda: now[0])


def test_health_from_a_checkout_behind_the_roles_merge_is_unreadable(
    tmp_path, monkeypatch, capsys
):
    """No verdict from a stale role definition, even when the cluster reads healthy (#5912).

    The fixture role borrows `sonarr`, a real k8s `containers_list` entry, because the gate
    resolves only such a role. Its workload is healthy, so a probe that gates from the stale
    definition exits 0: a green verdict about a role this checkout does not hold.
    """
    scrub_process_git_env(monkeypatch)
    _repo, roles_dir, _a, _b = _behind_origin(tmp_path, role="sonarr")
    (roles_dir / "sonarr" / "files" / "upstream").mkdir(parents=True)
    (roles_dir / "sonarr" / "files" / "upstream" / "app.yaml").write_text(
        _deployment("controller", "homelab")
    )
    code = health.run_health(
        "sonarr",
        cluster="prod",
        served="prod",
        tools=_healthy_cluster(),
        roles_dir=roles_dir,
        prom=_quiet_prom(),
        on_origin=lambda _r: True,
    )
    captured = capsys.readouterr()
    assert code == PROBE_UNREADABLE, captured.out
    assert "origin/master" in captured.err
    assert not [m for m in detach_notify.NOT_APPLICABLE_MARKERS if m in captured.err]


def test_a_squash_merged_branch_holding_origins_role_is_not_stale(
    tmp_path, monkeypatch
):
    """A session's branch after its PR squash-merged lacks the squash commit, not the role.

    `origin/master` carries the role change as a commit the branch never had, yet the role's
    files match. Refusing it would fail the post-merge gate for exactly the roles the PR changed.
    """
    scrub_process_git_env(monkeypatch)
    repo, roles_dir, a, _b = _behind_origin(tmp_path)
    upstream = {"ansible/roles/k8s/metallb/files/upstream/app.yaml": _deployment("x")}
    git(repo, "checkout", "-q", "--detach", a)
    squash = commit(repo, "squash of the branch", **upstream)
    git(repo, "update-ref", "refs/remotes/origin/master", squash)
    git(repo, "checkout", "-q", "--detach", a)
    commit(repo, "branch commit", **upstream)
    assert (
        health_checkout.stale_checkout_reason("metallb", roles_dir, lambda _r: True)
        is None
    )
