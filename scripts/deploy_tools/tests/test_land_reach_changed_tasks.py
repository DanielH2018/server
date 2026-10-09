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
from setup_role_diff import changed_task_texts

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


def _commit(repo, text: str, extra: dict[str, str | None] | None = None) -> str:
    """Write `text` as crony's tasks file in `repo`, commit it, and return the SHA.

    `extra` maps a further repo path to its content, or to None to delete it.
    """
    env = {"GIT_CONFIG_GLOBAL": "/dev/null", "HOME": str(repo), "PATH": "/usr/bin:/bin"}
    for path, content in {_TASKS: text, **(extra or {})}.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        if content is None:
            (repo / path).unlink()
        else:
            (repo / path).write_text(content)
    ident = ["-c", "user.name=t", "-c", "user.email=t@example.com"]
    run(["git", "add", "-A"], cwd=repo, check=True, env=env)
    run(
        ["git", *ident, "commit", "-q", "-m", "x", "--no-gpg-sign"],
        cwd=repo,
        check=True,
        env=env,
    )
    head = run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, env=env)
    return head.stdout.strip()


def _reach(tmp_path, path: str, before: dict, after: dict) -> frozenset[str]:
    """`setup_file_hosts` for `path` over a two-commit range from `before` to `after`.

    Each side maps a repo path to its content, or to None for a deleted file; crony's tasks
    file is `_TASKS`.
    """
    repo = tmp_path / "repo"
    env = {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "HOME": str(tmp_path),
        "PATH": "/usr/bin:/bin",
    }
    run(["git", "init", "-q", "-b", "master", str(repo)], check=True, env=env)
    old = _commit(repo, before.pop(_TASKS), before)
    new = _commit(repo, after.pop(_TASKS), after)
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
        path,
        playbook,
        all_vars,
        host_vars,
        roles_dir,
        pr_range=f"{old}..{new}",
        repo=repo,
    )


def _reach_of_an_edit(tmp_path, job: str) -> frozenset[str]:
    """The reach of crony's tasks file over a range whose only change is `job`'s task."""
    before, after = {_TASKS: _CRONS}, {_TASKS: _edit(_CRONS, job)}
    return _reach(tmp_path, _TASKS, before, after)


def test_an_edit_to_a_has_gitops_task_reaches_the_gitops_host_only(tmp_path):
    assert _reach_of_an_edit(tmp_path, "docs-refresh") == {"daniel-box"}


def test_an_edit_to_an_ungated_task_in_the_same_file_reaches_every_host(tmp_path):
    assert _reach_of_an_edit(tmp_path, "autoremove") == _ALL


_UNNAMED = "ansible/roles/setup/crony/templates/unnamed.j2"


def test_a_deleted_template_reaches_no_host(tmp_path):
    """#3890 deleted gitops_deploy's role-local alert template, which no task names."""
    before, after = {_TASKS: _CRONS, _UNNAMED: "x\n"}, {_TASKS: _CRONS, _UNNAMED: None}
    assert _reach(tmp_path, _UNNAMED, before, after) == frozenset()


def test_an_edited_template_no_task_names_keeps_the_role_reach(tmp_path):
    before, after = {_TASKS: _CRONS, _UNNAMED: "x\n"}, {_TASKS: _CRONS, _UNNAMED: "y\n"}
    assert _reach(tmp_path, _UNNAMED, before, after) == _ALL


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


def _pre_merge_checkout(tmp_path) -> tuple:
    """A repo whose range edits only a `has_gitops` task, with the checkout left at the start.

    deploy-ui runs `land.sh` from the primary checkout, which holds the pre-merge tree until
    the tick fast-forwards, so the checkout's task chains carry none of the changed tasks.
    """
    repo = tmp_path / "repo"
    env = {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "HOME": str(tmp_path),
        "PATH": "/usr/bin:/bin",
    }
    run(["git", "init", "-q", "-b", "master", str(repo)], check=True, env=env)
    tree = {
        "ansible/initial_setup.yml": yaml.safe_dump(
            [{"hosts": "all", "roles": ["crony"]}]
        ),
        "ansible/inventory/group_vars/all.yml": "has_gitops: false\n",
        "ansible/inventory/host_vars/daniel-box.yml": "has_gitops: true\n",
    }
    old = _commit(repo, _CRONS, tree)
    new = _commit(repo, _edit(_CRONS, "docs-refresh"))
    run(["git", "checkout", "-q", old], cwd=repo, check=True, env=env)
    return repo, old, new


def test_the_note_reads_the_merge_commits_tree_not_a_pre_merge_checkout(tmp_path):
    repo, old, new = _pre_merge_checkout(tmp_path)
    note = land_reach.remaining_setup_hosts_note(
        [_TASKS], "daniel-box", pr_range=f"{old}..{new}", ref=new, repo=repo
    )
    assert note == ""


def test_the_pre_merge_checkouts_own_tree_reads_wide(tmp_path):
    """The red half: the same range read from the checkout names the two other hosts."""
    repo, old, new = _pre_merge_checkout(tmp_path)
    ansible = repo / "ansible"
    note = land_reach._remaining_note(
        [_TASKS],
        "daniel-box",
        (),
        ansible / "initial_setup.yml",
        ansible / "inventory" / "group_vars" / "all.yml",
        ansible / "inventory" / "host_vars",
        ansible / "roles" / "setup",
        pr_range=f"{old}..{new}",
        repo=repo,
    )
    assert "daniel-pi" in note and "daniel-server" in note
