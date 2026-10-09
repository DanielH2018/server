"""A setup-role change names only the hosts its changed tasks, or its importers, run on (#3976).

PR #3971 edited box-only crons in `initial_setup/tasks/crons.yml`, a file that also holds
ungated crons, and the landing named an apply on daniel-server and daniel-pi. PR #3890 edited
`common/templates/unit-failure-alert.service.j2`, and `gitops_deploy`'s teardown half put the
same two hosts in the note, though only its `has_gitops` install renders the alert. Each case
below pairs the narrowed answer with one that must stay wide.

Run: uv run pytest scripts/deploy_tools/tests/test_land_reach_changed_tasks.py
"""

import yaml

import land_reach
from lib.proc_testing import run
from lib.repo_paths import REPO
from setup_role_chains import changed_task_texts

_CRONS = """\
- name: Weekly apt autoremove
  cron: {name: apt, job: autoremove}
- name: Refresh generated docs
  when: has_gitops
  cron: {name: docs, job: docs-refresh}
"""
_ALERT_TEMPLATE = "ansible/roles/setup/common/templates/unit-failure-alert.service.j2"
_ALL = frozenset({"daniel-box", "daniel-server", "daniel-pi"})


def _edit(text: str, job: str) -> str:
    return text.replace(f"job: {job}", f"job: {job} --new")


def test_an_edited_task_is_the_only_changed_text():
    changed = changed_task_texts(_CRONS, _edit(_CRONS, "docs-refresh"))
    assert changed is not None and len(changed) == 1
    assert "docs-refresh --new" in next(iter(changed))


def test_a_removed_task_or_a_changed_block_gate_stays_wide():
    removed = "\n".join(_CRONS.splitlines()[:2]) + "\n"
    assert changed_task_texts(_CRONS, removed) is None
    block = "- block:\n    - name: a\n      cron: {job: x}\n  when: has_gitops\n"
    assert changed_task_texts(block, block.replace("has_gitops", "true")) is None


_TASKS = "ansible/roles/setup/crony/tasks/main.yml"


def _commit(repo, text: str) -> str:
    """Write `text` as crony's tasks file in `repo`, commit it, and return the SHA."""
    env = {"GIT_CONFIG_GLOBAL": "/dev/null", "HOME": str(repo), "PATH": "/usr/bin:/bin"}
    (repo / _TASKS).parent.mkdir(parents=True, exist_ok=True)
    (repo / _TASKS).write_text(text)
    ident = ["-c", "user.name=t", "-c", "user.email=t@example.com"]
    run(["git", "add", _TASKS], cwd=repo, check=True, env=env)
    run(
        ["git", *ident, "commit", "-q", "-m", "x", "--no-gpg-sign"],
        cwd=repo,
        check=True,
        env=env,
    )
    head = run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, env=env)
    return head.stdout.strip()


def _reach_of_an_edit(tmp_path, job: str) -> frozenset[str]:
    """`setup_file_hosts` over a two-commit range whose only change is the task running `job`."""
    repo = tmp_path / "repo"
    env = {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "HOME": str(tmp_path),
        "PATH": "/usr/bin:/bin",
    }
    run(["git", "init", "-q", "-b", "master", str(repo)], check=True, env=env)
    old = _commit(repo, _CRONS)
    new = _commit(repo, _edit(_CRONS, job))
    roles_dir = repo / "ansible" / "roles" / "setup"
    playbook = tmp_path / "initial_setup.yml"
    playbook.write_text(yaml.safe_dump([{"hosts": "all", "roles": ["crony"]}]))
    all_vars = tmp_path / "all.yml"
    all_vars.write_text("has_gitops: false\n")
    host_vars = tmp_path / "host_vars"
    host_vars.mkdir()
    (host_vars / "daniel-box.yml").write_text("has_gitops: true\n")
    return land_reach.setup_file_hosts(
        "crony",
        _TASKS,
        playbook,
        all_vars,
        host_vars,
        roles_dir,
        pr_range=f"{old}..{new}",
        repo=repo,
    )


def test_an_edit_to_a_has_gitops_task_reaches_the_gitops_host_only(tmp_path):
    assert _reach_of_an_edit(tmp_path, "docs-refresh") == {"daniel-box"}


def test_an_edit_to_an_ungated_task_in_the_same_file_reaches_every_host(tmp_path):
    assert _reach_of_an_edit(tmp_path, "autoremove") == _ALL


def test_the_alert_template_reaches_gitops_deploy_through_its_install_half_only():
    assert (REPO / _ALERT_TEMPLATE).exists()
    assert land_reach.setup_role_hosts("gitops_deploy") == _ALL
    assert land_reach.setup_shipped_file_hosts("gitops_deploy", _ALERT_TEMPLATE) == {
        "daniel-box"
    }


def test_an_alert_template_change_names_only_an_importer_rendering_it_elsewhere():
    """claude_code imports `alert_unit.yml` for claude-rc with no gate, so daniel-server
    renders the alert too, and is the one host the note keeps."""
    note = land_reach.remaining_setup_hosts_note([_ALERT_TEMPLATE], "daniel-box")
    assert "`claude_code` also reaches daniel-server" in note
    assert note.count("also reaches") == 1, note


def test_a_role_gated_off_the_tick_host_is_left_to_the_plane_note():
    """`optimize_pi` runs only on daniel-pi, so the tick applies it nowhere (#3933).
    `plane_note` names its command; naming it here as well printed it twice."""
    files = ["ansible/roles/setup/optimize_pi/tasks/main.yml"]
    assert land_reach.setup_role_hosts("optimize_pi") == {"daniel-pi"}
    assert land_reach.remaining_setup_hosts_note(files, "daniel-box") == ""
