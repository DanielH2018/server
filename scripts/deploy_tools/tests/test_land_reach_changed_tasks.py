"""A setup-role change names only the hosts its changed tasks, or its importers, run on (#3976).

PR #3971 edited box-only crons in `initial_setup/tasks/crons.yml`, a file that also holds
ungated crons, and the landing named an apply on daniel-server and daniel-pi. PR #3890 edited
`common/templates/unit-failure-alert.service.j2`, and `gitops_deploy`'s teardown half put the
same two hosts in the note, though only its `has_gitops` install renders the alert. Each case
below pairs the narrowed answer with one that must stay wide.

Run: uv run pytest scripts/deploy_tools/tests/test_land_reach_changed_tasks.py
"""

import pytest
import yaml

from deploy_tools.land_lib import land_reach
from lib import yaml_fast
from lib.proc_testing import run
from lib.repo_paths import REPO
from deploy_tools.setup_lib.setup_role_chains import changed_task_texts

_CRONS = """\
- name: Weekly apt autoremove
  cron: {name: apt, job: autoremove}
- name: Refresh generated docs
  when: has_gitops
  cron: {name: docs, job: docs-refresh}
"""
_ALERT_TEMPLATE = "ansible/roles/setup/common/templates/unit-failure-alert.service.j2"
_ALL = frozenset({"daniel-box", "daniel-server", "daniel-pi"})
_CHECK_TIMER_TEMPLATES = tuple(
    f"ansible/roles/setup/common/templates/check-timer{name}.j2"
    for name in (".service", ".timer", "-boot.service")
)


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


_PACKAGED = (
    _CRONS
    + """\
- name: Refresh scanner data
  when: inventory_hostname not in crony_packaged_hosts['scan']
  cron: {name: scan, job: scan-update}
"""
)
_DEFAULTS = "ansible/roles/setup/crony/defaults/main.yml"
_EVERY_HOST_PACKAGED = (
    "crony_packaged_hosts:\n  scan: [daniel-pi, daniel-server, daniel-box]\n"
)


def _without(text: str, job: str) -> str:
    """`text` with the task whose cron runs `job` deleted."""
    tasks = yaml_fast.safe_load(text)
    return yaml.safe_dump(
        [t for t in tasks if t["cron"]["job"] != job], sort_keys=False
    )


def _reach_of_a_deletion(tmp_path, job: str, text: str = _PACKAGED) -> frozenset[str]:
    before = {_TASKS: text, _DEFAULTS: _EVERY_HOST_PACKAGED}
    after = {_TASKS: _without(text, job)}
    return _reach(tmp_path, _TASKS, before, after)


def test_deleting_a_task_every_hosts_package_skips_reaches_no_host(tmp_path):
    """#7017 deleted an `initial_setup` task its packaged-hosts guard skipped on every host,
    from a file that also holds ungated tasks, and the landing owed two hosts an apply."""
    assert _reach_of_a_deletion(tmp_path, "scan-update") == frozenset()


def test_deleting_a_has_gitops_task_reaches_the_gitops_host_only(tmp_path):
    assert _reach_of_a_deletion(tmp_path, "docs-refresh") == {"daniel-box"}


def test_an_edit_to_a_task_every_hosts_package_skips_reaches_no_host(tmp_path):
    """The guard's map lives in the role's defaults, which the gate reader must load."""
    before = {_TASKS: _PACKAGED, _DEFAULTS: _EVERY_HOST_PACKAGED}
    after = {_TASKS: _edit(_PACKAGED, "scan-update")}
    assert _reach(tmp_path, _TASKS, before, after) == frozenset()


_IMPORTED = "ansible/roles/setup/crony/tasks/scans.yml"
_SCANS = """\
- name: Weekly scan
  cron: {name: scan, job: scan}
- name: Server-only scan
  when: inventory_hostname == 'daniel-server'
  cron: {name: server-scan, job: server-scan}
"""


def test_a_deleted_task_in_an_imported_file_keeps_the_gate_above_the_import(tmp_path):
    """integrity.yml is imported from main.yml, so the deleted task's chain starts with the
    import's gates. The server-only task sits under a box-only block, so it ran nowhere; the
    whole file's reach is {daniel-box}, and dropping the block gate gives {daniel-server}."""
    main = _CRONS + "- block:\n    - import_tasks: scans.yml\n  when: has_gitops\n"
    before = {_TASKS: main, _IMPORTED: _SCANS}
    after = {_TASKS: main, _IMPORTED: _without(_SCANS, "server-scan")}
    assert _reach(tmp_path, _IMPORTED, before, after) == frozenset()


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


@pytest.mark.parametrize("template", _CHECK_TIMER_TEMPLATES)
def test_a_check_timer_template_reaches_no_gitops_deploy_host(template):
    """teardown.yml imports `check_timer.yml` with state absent, so it renders nothing (#7416).

    install.yml rendered the GitHub checks' timers until H7.37 moved them to a component.
    """
    assert land_reach.setup_shipped_file_hosts("gitops_deploy", template) == frozenset()
    note = land_reach.remaining_setup_hosts_note([template], "daniel-box")
    assert "`gitops_deploy`" not in note, note


@pytest.mark.parametrize("template", _CHECK_TIMER_TEMPLATES)
def test_a_check_timer_template_names_initial_setup_only_on_the_listed_hosts(template):
    assert land_reach.setup_shipped_file_hosts("initial_setup", template) == {
        "daniel-box",
        "daniel-server",
    }


def test_a_check_timer_task_file_change_keeps_the_teardown_hosts():
    """The file's absent arm runs on the hosts that render nothing, so its own edit owes them."""
    tasks = "ansible/roles/setup/common/tasks/check_timer.yml"
    assert land_reach.setup_shipped_file_hosts("gitops_deploy", tasks) == _ALL


@pytest.mark.parametrize(
    "state, hosts",
    [
        pytest.param("absent", frozenset(), id="literal-absent"),
        pytest.param("present", _ALL, id="literal-present"),
        pytest.param(
            "{{ 'present' if inventory_hostname in on_list else 'absent' }}",
            {"daniel-server"},
            id="per-host",
        ),
        pytest.param("{{ state_var }}", frozenset(), id="group-var"),
        pytest.param("{{ other | default('x') }}", _ALL, id="filter-stays-wide"),
        pytest.param("{{ '{{' }}-{{ state_var }}", _ALL, id="partial-jinja-stays-wide"),
        pytest.param(["{{ state_var }}"], _ALL, id="jinja-in-a-list-stays-wide"),
    ],
)
def test_an_owner_render_is_read_with_the_imports_vars(tmp_path, state, hosts):
    (tmp_path / "initial_setup.yml").write_text("- hosts: x\n  roles: [demo]\n")
    # A group var of the gate's own name: an import var read as unknown must not fall back to it.
    (tmp_path / "all.yml").write_text(
        "on_list: [daniel-server]\nstate_var: absent\nshared_state: absent\n"
    )
    (tmp_path / "host_vars").mkdir()
    for path, tasks in (
        (
            "common/tasks/shared.yml",
            [
                {
                    "name": "Render",
                    "when": "shared_state == 'present'",
                    "template": {"src": "x/shared.j2", "dest": "/etc/shared"},
                }
            ],
        ),
        (
            "demo/tasks/main.yml",
            [
                {
                    "name": "Import",
                    "import_tasks": "{{ role_path }}/../common/tasks/shared.yml",
                    "vars": {"shared_state": state},
                }
            ],
        ),
    ):
        (tmp_path / "roles" / path).parent.mkdir(parents=True)
        (tmp_path / "roles" / path).write_text(yaml.safe_dump(tasks))
    assert (
        land_reach.setup_shipped_file_hosts(
            "demo",
            "ansible/roles/setup/common/templates/shared.j2",
            tmp_path / "initial_setup.yml",
            tmp_path / "all.yml",
            tmp_path / "host_vars",
            tmp_path / "roles",
        )
        == hosts
    )


def test_an_alert_template_change_names_only_an_importer_rendering_it_elsewhere():
    """claude_code imports `alert_unit.yml` for claude-rc with no gate. daniel-server ends the
    role (the component guard), so it takes the alert through its package, and the note
    names no host for claude_code."""
    note = land_reach.remaining_setup_hosts_note([_ALERT_TEMPLATE], "daniel-box")
    assert "`claude_code`" not in note, note


def test_a_role_gated_off_the_tick_host_is_left_to_the_plane_note():
    """`optimize_pi` is gated to daniel-pi, so the tick applies it nowhere (#3933), and its
    `end_role` stub (H7.25) leaves it no host to name. `plane_note` names a command for a gated
    role; naming it here as well printed it twice."""
    files = ["ansible/roles/setup/optimize_pi/tasks/main.yml"]
    assert land_reach.setup_role_hosts("optimize_pi") == frozenset()
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
            [{"hosts": "all", "roles": [{"role": "crony", "tags": ["crony"]}]}]
        ),
        "ansible/inventory/group_vars/all.yml": "has_gitops: false\n",
        "ansible/inventory/host_vars/daniel-box.yml": "has_gitops: true\n",
    }
    old = _commit(repo, _CRONS, tree)
    new = _commit(repo, _edit(_CRONS, "docs-refresh"))
    run(["git", "checkout", "-q", old], cwd=repo, check=True, env=env)
    return repo, old, new


def _note(repo, old: str, new: str, ref: str = "") -> str:
    """The owed-host note for the range's crony edit, read in `repo`."""
    return land_reach.remaining_setup_hosts_note(
        [_TASKS], "daniel-box", pr_range=f"{old}..{new}", ref=ref, repo=repo
    )


def test_the_note_reads_the_merge_commits_tree_not_a_pre_merge_checkout(tmp_path):
    """Read at the merge commit, the box-only edit owes no other host an apply.

    The negative control reads the same range from the checkout, which predates it, and must
    name the two other hosts. Without it the empty note also passed when `tree_at` fell back
    and when no `ref` reached it, because the fallback read this repo's tree, where crony is
    in no playbook (#4084).
    """
    repo, old, new = _pre_merge_checkout(tmp_path)
    unread = _note(repo, old, new)
    assert "daniel-pi" in unread and "daniel-server" in unread, unread
    assert _note(repo, old, new, ref=new) == ""


@pytest.mark.parametrize("ref", ["", "0" * 40], ids=["no-ref", "unarchivable-ref"])
def test_a_tree_without_the_changed_tasks_says_the_note_stays_wide(
    tmp_path, capsys, monkeypatch, ref
):
    """A checkout that predates the merge holds none of the changed tasks, so the note keeps
    the whole file's reach. It must say so, not print the wide note as if it were narrowed.

    The line names the script running, as a warning, not a borrowed `land_reach:` (#4478)."""
    monkeypatch.setattr("sys.argv", ["/repo/scripts/deploy_tools/land.py"])
    repo, old, new = _pre_merge_checkout(tmp_path)
    note = _note(repo, old, new, ref=ref)
    assert "daniel-pi" in note and "daniel-server" in note, note
    err = capsys.readouterr().err
    assert f"reach 0 of the 1 tasks {old[:8]}..{new[:8]} changed in {_TASKS}" in err
    assert "land.py: warning: the task chains read from" in err, err
    assert ("could not read" in err) == bool(ref), err


def test_the_tick_owes_an_initial_setup_edit_to_the_other_hosts():
    """The pair the GitOps tick records in its `owed` ledger (#6510): it ran the role here."""
    reach = land_reach.remaining_setup_hosts(
        ["ansible/roles/setup/initial_setup/tasks/main.yml"], "daniel-box"
    )
    assert "daniel-server" in reach["initial_setup"]
    assert "daniel-box" not in reach["initial_setup"]


def test_the_tick_owes_nothing_for_a_role_only_its_own_host_runs():
    """The rejecting half: `gitops_deploy` installs on the deployer alone."""
    reach = land_reach.remaining_setup_hosts(
        ["ansible/roles/setup/gitops_deploy/files/gitops_markers.py"], "daniel-box"
    )
    assert "gitops_deploy" not in reach
