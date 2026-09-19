"""check_stale_composes(), the rendered-but-undeclared watchdog, exercised by calling it.

It pages once per distinct stale set and clears its marker when the set empties. A missing
`containers/` IS an empty set (#2021): daniel-box has had none since the 2026-08-14
migration, and reading that as unreadable left its Docker-era `configarr` marker in place
for good — where a future stale set of exactly that name would have deduped against it. An
unreadable inventory or directory still leaves the marker alone. Every test runs against
the canned config and the tmp state dir from conftest.py; `_posts` and `_marker` are the
alert-channel helpers in test_gitops_deploy_alert_channels.py.
"""

# ansible/roles/setup/gitops_deploy/tests/test_gitops_deploy_stale_composes.py

import dataclasses
import pathlib

import deploy_alerts
from test_gitops_deploy_alert_channels import _marker, _posts


# ── check_stale_composes(): the rendered-but-undeclared watchdog ──────────────────────────────
def _repo_with(
    tmp_path: pathlib.Path, rendered: list[str], declared: str | None
) -> pathlib.Path:
    """Build a fake checkout carrying rendered composes and this host's host_vars.

    A rendered compose per name in `rendered`, and this host's host_vars holding `declared` (None
    leaves the file absent).
    """
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
    gitops_deploy, state_dir, tmp_path
):
    tools, seen = _posts(state_dir)
    repo = _repo_with(tmp_path, ["sonarr", "configarr"], DECLARES_SONARR)
    settings = dataclasses.replace(gitops_deploy.tick_config(), repo=str(repo))
    deploy_alerts.check_stale_composes(tools, gitops_deploy.STATE, settings)
    deploy_alerts.check_stale_composes(tools, gitops_deploy.STATE, settings)
    ((key, content),) = seen
    assert key == "stale-composes:configarr"
    assert "`configarr`" in content and "test-host" in content
    assert _marker(state_dir, "stale_composes_alerted") == "configarr"


def test_a_grown_stale_set_pages_again_and_a_cleared_one_resets(
    gitops_deploy, state_dir, tmp_path
):
    tools, seen = _posts(state_dir)
    repo = _repo_with(tmp_path, ["sonarr", "configarr"], DECLARES_SONARR)
    settings = dataclasses.replace(gitops_deploy.tick_config(), repo=str(repo))
    deploy_alerts.check_stale_composes(tools, gitops_deploy.STATE, settings)
    (repo / "containers" / "kopia").mkdir()
    (repo / "containers" / "kopia" / "docker-compose.yml").write_text("services: {}\n")
    deploy_alerts.check_stale_composes(tools, gitops_deploy.STATE, settings)
    assert [key for key, _ in seen] == [
        "stale-composes:configarr",
        "stale-composes:configarr,kopia",
    ]
    for svc in ("configarr", "kopia"):
        (repo / "containers" / svc / "docker-compose.yml").unlink()
    deploy_alerts.check_stale_composes(tools, gitops_deploy.STATE, settings)
    assert len(seen) == 2, "an empty stale set is not an alert"
    assert _marker(state_dir, "stale_composes_alerted") is None


def test_a_k8s_entry_does_not_hide_a_leftover_render(
    gitops_deploy, state_dir, tmp_path
):
    # A service that migrated to k8s keeps its containers_list entry with platform: k8s; its
    # rendered compose on this host is exactly the stale dir the watchdog exists for.
    tools, seen = _posts(state_dir)
    declared = "containers_list:\n  - name: configarr\n    platform: k8s\n"
    repo = _repo_with(tmp_path, ["configarr"], declared)
    settings = dataclasses.replace(gitops_deploy.tick_config(), repo=str(repo))
    deploy_alerts.check_stale_composes(tools, gitops_deploy.STATE, settings)
    assert [key for key, _ in seen] == ["stale-composes:configarr"]


def test_an_unreadable_inventory_is_not_this_watchdogs_page(
    gitops_deploy, state_dir, tmp_path
):
    tools, seen = _posts(state_dir)
    repo = _repo_with(tmp_path, ["configarr"], None)
    settings = dataclasses.replace(gitops_deploy.tick_config(), repo=str(repo))
    deploy_alerts.check_stale_composes(tools, gitops_deploy.STATE, settings)
    assert seen == []
    assert _marker(state_dir, "stale_composes_alerted") is None


def test_a_host_with_no_containers_dir_clears_a_left_over_marker(
    gitops_deploy, state_dir, tmp_path
):
    """A missing `containers/` is an empty stale set, so an old marker clears (#2021).

    daniel-box has had no `containers/` since the 2026-08-14 migration, and read that as
    "unreadable" every tick, so the Docker-era `configarr` marker it had paged on was never
    cleared — and a future stale set of exactly `configarr` would have deduped against it.
    """
    tools, seen = _posts(state_dir)
    repo = _repo_with(tmp_path, [], DECLARES_SONARR)
    assert not (repo / "containers").exists()
    settings = dataclasses.replace(gitops_deploy.tick_config(), repo=str(repo))
    (state_dir / "stale_composes_alerted").write_text("configarr")
    deploy_alerts.check_stale_composes(tools, gitops_deploy.STATE, settings)
    assert seen == [], "an empty stale set is not an alert"
    assert _marker(state_dir, "stale_composes_alerted") is None


def test_an_unreadable_containers_dir_leaves_the_marker_alone(
    gitops_deploy, state_dir, tmp_path
):
    """The rejecting half: absent clears, unreadable does not.

    A directory this uid cannot list is still not evidence that nothing is stale, so the
    marker stays and nothing pages — the same silence an unreadable inventory gets.
    """
    tools, seen = _posts(state_dir)
    repo = _repo_with(tmp_path, ["configarr"], DECLARES_SONARR)
    settings = dataclasses.replace(gitops_deploy.tick_config(), repo=str(repo))
    (state_dir / "stale_composes_alerted").write_text("configarr")
    (repo / "containers").chmod(0o000)
    try:
        deploy_alerts.check_stale_composes(tools, gitops_deploy.STATE, settings)
    finally:
        (repo / "containers").chmod(0o700)
    assert seen == []
    assert _marker(state_dir, "stale_composes_alerted") == "configarr"
