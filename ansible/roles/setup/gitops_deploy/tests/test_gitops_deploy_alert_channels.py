"""The per-channel alert helpers and the two disk watchdogs, exercised by calling them.

alert_once() advances its per-SHA marker on DETECTION, not delivery, and hands the post to
deliver(): a webhook blip is redelivered by the queue, and an ff-merged path that noops on
the next tick does not re-page. alert_deferred() and alert_secrets_deferred() choose the
channel and the text; the tasks and meta channels subtract what this tick deployed, the k8s
channel never does. check_stale_composes() pages once per distinct stale set and clears its
marker when the set empties. _record_behind() stamps the behind-origin marker once and keeps
the first-seen time across later pushes, and never lets a git failure page. Every test runs
against the canned config and the tmp state dir from conftest.py; main()'s own branches
are in test_gitops_deploy_main_branches.py.
"""

# ansible/roles/setup/gitops_deploy/tests/test_gitops_deploy_alert_channels.py

import ast
import json
import pathlib

import pytest
from deploy_changes import ChangeSet
from deploy_remediation import k8s_remediation

ORIGIN = "a" * 40
LATER = "b" * 40


def _posts(gitops_deploy, monkeypatch, delivered: bool = True) -> list[tuple[str, str]]:
    """Replace deliver() with one that records (key, content) and reports `delivered`."""
    seen: list[tuple[str, str]] = []

    def fake_deliver(key: str, content: str) -> bool:
        seen.append((key, content))
        return delivered

    monkeypatch.setattr(gitops_deploy, "deliver", fake_deliver)
    return seen


def _marker(state_dir: pathlib.Path, name: str) -> str | None:
    path = state_dir / name
    return path.read_text().strip() if path.exists() else None


# ── alert_once(): the per-SHA dedupe ──────────────────────────────────────────────────────────
def test_alert_once_delivers_and_advances_the_marker(
    gitops_deploy, monkeypatch, state_dir
):
    seen = _posts(gitops_deploy, monkeypatch)
    gitops_deploy.alert_once(gitops_deploy.TASKS_ALERT_FILE, "tasks", ORIGIN, "changed")
    assert seen == [(f"tasks:{ORIGIN}", "changed")]
    assert _marker(state_dir, "tasks_alerted_sha") == ORIGIN


def test_alert_once_is_silent_for_a_sha_already_alerted(
    gitops_deploy, monkeypatch, state_dir
):
    seen = _posts(gitops_deploy, monkeypatch)
    for _ in range(3):
        gitops_deploy.alert_once(
            gitops_deploy.TASKS_ALERT_FILE, "tasks", ORIGIN, "changed"
        )
    assert len(seen) == 1
    gitops_deploy.alert_once(gitops_deploy.TASKS_ALERT_FILE, "tasks", LATER, "again")
    assert seen[-1] == (f"tasks:{LATER}", "again")
    assert _marker(state_dir, "tasks_alerted_sha") == LATER


def test_alert_once_marks_detection_not_delivery(gitops_deploy, monkeypatch, state_dir):
    # A failed post must not re-page on the next tick through this path: the marker advances
    # anyway, and redelivery is the pending queue's job. Real deliver(), fake discord().
    monkeypatch.setattr(gitops_deploy, "discord", lambda _content: False)
    gitops_deploy.alert_once(gitops_deploy.META_ALERT_FILE, "meta", ORIGIN, "changed")
    assert _marker(state_dir, "meta_alerted_sha") == ORIGIN
    queued = json.loads((state_dir / "pending_alerts.json").read_text())
    assert queued == {f"meta:{ORIGIN}": "changed"}


# ── alert_secrets_deferred() ──────────────────────────────────────────────────────────────────
def test_a_secrets_change_pages_once_naming_the_sha(
    gitops_deploy, monkeypatch, state_dir
):
    seen = _posts(gitops_deploy, monkeypatch)
    gitops_deploy.alert_secrets_deferred(ORIGIN, ChangeSet(secrets=True))
    gitops_deploy.alert_secrets_deferred(ORIGIN, ChangeSet(secrets=True))
    ((key, content),) = seen
    assert key == f"secrets:{ORIGIN}"
    assert ORIGIN[:8] in content and "nothing was redeployed" in content
    assert _marker(state_dir, "secrets_alerted_sha") == ORIGIN


def test_no_secrets_change_pages_nothing(gitops_deploy, monkeypatch, state_dir):
    seen = _posts(gitops_deploy, monkeypatch)
    gitops_deploy.alert_secrets_deferred(ORIGIN, ChangeSet(services={"sonarr"}))
    assert seen == []
    assert _marker(state_dir, "secrets_alerted_sha") is None


# ── alert_deferred(): tasks, meta and k8s channels ────────────────────────────────────────────
def test_an_empty_changeset_pages_nothing(gitops_deploy, monkeypatch, state_dir):
    seen = _posts(gitops_deploy, monkeypatch)
    gitops_deploy.alert_deferred(ORIGIN, set(), ChangeSet())
    assert seen == []
    assert not any(p.name.endswith("_alerted_sha") for p in state_dir.iterdir())


def test_tasks_and_meta_name_only_what_this_tick_did_not_deploy(
    gitops_deploy, monkeypatch, state_dir
):
    # A combined push: svcA's template rode its scoped redeploy, svcB's tasks/ and svcC's
    # meta/deps.yml did not.
    seen = _posts(gitops_deploy, monkeypatch)
    cs = ChangeSet(services={"svca"}, tasks={"svca", "svcb"}, meta={"svcc"})
    gitops_deploy.alert_deferred(ORIGIN, {"svca"}, cs)
    by_key = dict(seen)
    assert set(by_key) == {f"tasks:{ORIGIN}", f"meta:{ORIGIN}"}
    assert "`svcb`" in by_key[f"tasks:{ORIGIN}"]
    assert "svca" not in by_key[f"tasks:{ORIGIN}"]
    assert "`svcc`" in by_key[f"meta:{ORIGIN}"]
    assert _marker(state_dir, "tasks_alerted_sha") == ORIGIN
    assert _marker(state_dir, "meta_alerted_sha") == ORIGIN


def test_a_structural_change_that_rode_its_own_redeploy_is_not_flagged(
    gitops_deploy, monkeypatch, state_dir
):
    seen = _posts(gitops_deploy, monkeypatch)
    cs = ChangeSet(services={"svca"}, tasks={"svca"})
    gitops_deploy.alert_deferred(ORIGIN, {"svca"}, cs)
    assert seen == []


def test_a_k8s_change_pages_with_the_remediation_for_this_host(
    gitops_deploy, monkeypatch, state_dir
):
    seen = _posts(gitops_deploy, monkeypatch)
    cs = ChangeSet(k8s={"sonarr"})
    gitops_deploy.alert_deferred(ORIGIN, set(), cs, declared_k8s={"sonarr"})
    ((key, content),) = seen
    assert key == f"k8s:{ORIGIN}"
    assert "`sonarr`" in content and ORIGIN[:8] in content
    assert content.endswith(k8s_remediation({"sonarr"}, {"sonarr"}, set()))
    assert _marker(state_dir, "k8s_alerted_sha") == ORIGIN


def test_a_k8s_change_is_flagged_even_when_something_else_deployed(
    gitops_deploy, monkeypatch, state_dir
):
    # Unlike tasks/meta there is no `- deployed` subtraction: this deployer never applies a k8s
    # role through deploy(cs.services), so nothing a k8s change could have ridden.
    seen = _posts(gitops_deploy, monkeypatch)
    gitops_deploy.alert_deferred(
        ORIGIN, {"sonarr"}, ChangeSet(services={"sonarr"}, k8s={"sonarr"}), {"sonarr"}
    )
    assert [key for key, _ in seen] == [f"k8s:{ORIGIN}"]


def test_an_unread_inventory_prescribes_the_full_deploy(
    gitops_deploy, monkeypatch, state_dir
):
    # declared_k8s=None is the caller that has not read host_vars. Treated as the empty set,
    # every changed role reads as untaggable and the remediation is the full deploy: slower,
    # but a `--tags` line for a role with no entry exits 0 having applied nothing.
    seen = _posts(gitops_deploy, monkeypatch)
    gitops_deploy.alert_deferred(ORIGIN, set(), ChangeSet(k8s={"sonarr"}))
    ((_key, content),) = seen
    assert content.endswith(k8s_remediation({"sonarr"}, set(), set()))


# ── check_stale_composes(): the rendered-but-undeclared watchdog ──────────────────────────────
def _repo_with(
    tmp_path: pathlib.Path, rendered: list[str], declared: str | None
) -> pathlib.Path:
    """A fake checkout: a rendered compose per name in `rendered`, and this host's host_vars
    holding `declared` (None leaves the file absent)."""
    repo = tmp_path / "repo"
    for svc in rendered:
        (repo / "containers" / svc).mkdir(parents=True)
        (repo / "containers" / svc / "docker-compose.yml").write_text("services: {}\n")
    if declared is not None:
        hostvars = repo / "ansible" / "inventory" / "host_vars" / "test-host.yml"
        hostvars.parent.mkdir(parents=True, exist_ok=True)
        hostvars.write_text(declared)
    return repo


DECLARES_SONARR = "containers_list:\n  - name: sonarr\n    platform: docker\n"


def test_a_stale_compose_pages_once_per_distinct_set(
    gitops_deploy, monkeypatch, state_dir, tmp_path
):
    seen = _posts(gitops_deploy, monkeypatch)
    repo = _repo_with(tmp_path, ["sonarr", "configarr"], DECLARES_SONARR)
    monkeypatch.setattr(gitops_deploy, "REPO", str(repo))
    gitops_deploy.check_stale_composes()
    gitops_deploy.check_stale_composes()
    ((key, content),) = seen
    assert key == "stale-composes:configarr"
    assert "`configarr`" in content and "test-host" in content
    assert _marker(state_dir, "stale_composes_alerted") == "configarr"


def test_a_grown_stale_set_pages_again_and_a_cleared_one_resets(
    gitops_deploy, monkeypatch, state_dir, tmp_path
):
    seen = _posts(gitops_deploy, monkeypatch)
    repo = _repo_with(tmp_path, ["sonarr", "configarr"], DECLARES_SONARR)
    monkeypatch.setattr(gitops_deploy, "REPO", str(repo))
    gitops_deploy.check_stale_composes()
    (repo / "containers" / "kopia").mkdir()
    (repo / "containers" / "kopia" / "docker-compose.yml").write_text("services: {}\n")
    gitops_deploy.check_stale_composes()
    assert [key for key, _ in seen] == [
        "stale-composes:configarr",
        "stale-composes:configarr,kopia",
    ]
    for svc in ("configarr", "kopia"):
        (repo / "containers" / svc / "docker-compose.yml").unlink()
    gitops_deploy.check_stale_composes()
    assert len(seen) == 2, "an empty stale set is not an alert"
    assert _marker(state_dir, "stale_composes_alerted") is None


def test_a_k8s_entry_does_not_hide_a_leftover_render(
    gitops_deploy, monkeypatch, state_dir, tmp_path
):
    # A service that migrated to k8s keeps its containers_list entry with platform: k8s; its
    # rendered compose on this host is exactly the stale dir the watchdog exists for.
    seen = _posts(gitops_deploy, monkeypatch)
    declared = "containers_list:\n  - name: configarr\n    platform: k8s\n"
    repo = _repo_with(tmp_path, ["configarr"], declared)
    monkeypatch.setattr(gitops_deploy, "REPO", str(repo))
    gitops_deploy.check_stale_composes()
    assert [key for key, _ in seen] == ["stale-composes:configarr"]


def test_an_unreadable_inventory_is_not_this_watchdogs_page(
    gitops_deploy, monkeypatch, state_dir, tmp_path
):
    seen = _posts(gitops_deploy, monkeypatch)
    repo = _repo_with(tmp_path, ["configarr"], None)
    monkeypatch.setattr(gitops_deploy, "REPO", str(repo))
    gitops_deploy.check_stale_composes()
    assert seen == []
    assert _marker(state_dir, "stale_composes_alerted") is None


# ── _record_behind(): the behind-origin marker ────────────────────────────────────────────────
def _git_heads(
    gitops_deploy, monkeypatch, local: str, origin: str, behind: bool
) -> None:
    def fake_run(argv, **_kwargs):
        assert argv[:2] == ["git", "rev-parse"], argv
        return origin if argv[2].startswith("origin/") else local

    monkeypatch.setattr(gitops_deploy, "run", fake_run)
    monkeypatch.setattr(gitops_deploy, "is_ancestor", lambda _a, _d: behind)


def test_a_tick_that_ended_behind_stamps_first_seen_once(
    gitops_deploy, monkeypatch, state_dir
):
    _git_heads(gitops_deploy, monkeypatch, "local", ORIGIN, behind=True)
    gitops_deploy._record_behind()
    sha, first_seen = _marker(state_dir, "behind_since").split()
    assert sha == ORIGIN
    # A later push to a still-stuck host refreshes the SHA and keeps the clock.
    _git_heads(gitops_deploy, monkeypatch, "local", LATER, behind=True)
    gitops_deploy._record_behind()
    assert _marker(state_dir, "behind_since") == f"{LATER} {first_seen}"


def test_convergence_clears_the_marker(gitops_deploy, monkeypatch, state_dir):
    (state_dir / "behind_since").write_text(f"{ORIGIN} 1700000000")
    _git_heads(gitops_deploy, monkeypatch, ORIGIN, ORIGIN, behind=False)
    gitops_deploy._record_behind()
    assert _marker(state_dir, "behind_since") is None


def test_a_git_failure_here_logs_and_leaves_the_marker(
    gitops_deploy, monkeypatch, state_dir, capsys
):
    # The tick has already done its work; a rev-parse error must not turn it into a crash page.
    (state_dir / "behind_since").write_text(f"{ORIGIN} 1700000000")

    def broken_run(_argv, **_kwargs):
        raise RuntimeError("git rev-parse HEAD -> 128")

    monkeypatch.setattr(gitops_deploy, "run", broken_run)
    gitops_deploy._record_behind()
    assert "could not record behind-origin state" in capsys.readouterr().out
    assert _marker(state_dir, "behind_since") == f"{ORIGIN} 1700000000"


# ── the state_dir fixture covers every state path the module names ────────────────────────────
def test_state_dir_repoints_every_state_path_in_the_module(
    gitops_deploy, gitops_tree, state_dir
):
    """The fixture patches module constants whose value starts with the state prefix. A path
    built any other way (an f-string, os.path.join) would keep pointing at the host and the
    test writing through it would pass against /var/lib. Every string literal naming the
    prefix must therefore be a module-level constant, and after the fixture none may remain."""
    prefix = "/var/lib/gitops-deploy"
    literals = {
        node.value
        for node in ast.walk(gitops_tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and prefix in node.value
    }
    module_level = {
        node.value.value
        for node in gitops_tree.body
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
    }
    assert literals and literals <= module_level, (
        f"state paths not assigned at module level: {sorted(literals - module_level)}"
    )
    live = {
        name: value
        for name, value in vars(gitops_deploy).items()
        if isinstance(value, str) and value.startswith(prefix)
    }
    assert not live, f"state_dir left these on the host: {live}"


@pytest.mark.parametrize("name", ["LAST_RUN", "PENDING_ALERTS_FILE", "HOLD_FILE"])
def test_state_dir_keeps_each_markers_basename(gitops_deploy, state_dir, name):
    assert getattr(gitops_deploy, name).startswith(str(state_dir) + "/")
