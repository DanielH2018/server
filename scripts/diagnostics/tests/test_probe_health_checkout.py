"""`probe.py health <tag>` run from a checkout that predates the tag's k8s role (#5909).

A worktree six hours behind master had no `roles/k8s/metallb/`, so the probe guessed a workload
named `metallb` in the default namespace and failed a healthy MetalLB. The role existed on
`origin/master`, which is what lets the probe tell "this checkout is stale" from "not a k8s tag".

Run: uv run pytest scripts/diagnostics/tests/test_probe_health_checkout.py
"""

from deploy_tools.deploy_lib import detach_notify
from diagnostics.probe_lib import health, health_checkout
from lib.exit_codes import PROBE_UNREADABLE
from lib.git_testing import commit, git, init_repo


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
