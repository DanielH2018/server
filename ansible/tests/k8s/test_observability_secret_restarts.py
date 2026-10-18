"""One observability Secret edit restarts the workloads that read that Secret, and no others.

`k8s/manifests` registers `manifests_secret_render` over every Secret file the role renders,
so `is changed` on it fires for any one of them. observability pairs each result's `changed`
flag with its file, as `observability_changed_secret_files`, and two tasks read that fact:
the private restart and the restart-count snapshot. Each entry of
`observability_stabilise_workloads` names the files its pod reads in `restart_secrets`.

A rotated Grafana admin password reaches the database through that restart: grafana's
initContainer runs `grafana cli admin reset-admin-password` before every start, and the role
runs no CLI of its own.

The pairs below render each task's own `when` against the role's real defaults. The first
test runs a real `no_log` loop through `ansible-playbook`, because the pairing is only as good
as the `changed` flag a censored loop leaves in its register.

The entries carry no `rolled_by_role`: the shared role drops an entry carrying it from the
pod-template fingerprints, and the restart's "the apply already rolled it" skip reads those
(#3115). The last pair holds that against the shared role's real expression.

Run: uv run pytest ansible/tests/k8s/test_observability_secret_restarts.py
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from lib import yaml_fast
from lib.git_testing import scrubbed_env

from _helpers import ANSIBLE, load_tasks, load_yaml, render_expr, task_named, walk_tasks
from _release_expectation import (
    MAIN,
    OBSERVABILITY,
    include_role_vars,
    observability_self_rollouts,
)
from _release_expectation import rollouts as stamped_rollouts
from _k8s_render import pod_spec, render_role_template

TASKS = load_tasks(OBSERVABILITY / "tasks/main.yml")
WORKLOADS = load_yaml(OBSERVABILITY / "defaults/main.yml")[
    "observability_stabilise_workloads"
]
SECRET_READERS = frozenset({"prometheus", "grafana", "alertmanager"})

CHECK = task_named(TASKS, "Check that every Secret render reported")
CHANGED_FILES = task_named(TASKS, "List the Secret files whose render changed")
RESTART = task_named(TASKS, "Restart the telemetry workloads whose secret changed")
AMEND = task_named(TASKS, "Record the telemetry workloads the secret restart rolled")
SNAPSHOT = task_named(TASKS, "Snapshot the telemetry workloads' restart counts")
RENDER = task_named(MAIN, "Render secret manifests")
AMEND_REWRITE = task_named(
    load_tasks(ANSIBLE / "roles/k8s/manifests/tasks/rollout_amend.yml"),
    "Rewrite the rollout expectation",
)


def _changed_files(files: list[str], results: list[dict]) -> list[str]:
    return render_expr(
        CHANGED_FILES["ansible.builtin.set_fact"]["observability_changed_secret_files"],
        manifests_secret_files_resolved=files,
        manifests_secret_render={"results": results},
    )


def _check_passes(files: list[str], results: list[dict]) -> bool:
    return all(
        render_expr(
            "{{ " + clause + " }}",
            manifests_secret_files_resolved=files,
            manifests_secret_render={"results": results},
        )
        for clause in CHECK["ansible.builtin.assert"]["that"]
    )


pytestmark_playbook = pytest.mark.skipif(
    shutil.which("ansible-playbook") is None, reason="ansible-playbook not on PATH"
)


@pytestmark_playbook
def test_a_no_log_secret_render_still_names_the_file_that_changed(
    tmp_path: Path,
) -> None:
    """A real `no_log` loop, then the role's own check and fact over its register.

    One file is rendered over identical bytes, one over different bytes and one fresh. Only
    the last two may be selected. If `no_log` stripped `changed`, the check would fail the
    play; if it censored the flag to false, the selection would come back empty.
    """
    assert RENDER["no_log"] is True, "the shared secret render is no longer no_log"
    files = ["same-secret.yaml", "edited-secret.yaml", "new-secret.yaml"]
    (tmp_path / "templates").mkdir()
    (tmp_path / "staged").mkdir()
    for name in files:
        (tmp_path / "templates" / f"{name}.j2").write_text(
            f"kind: Secret # {name} v2\n"
        )
    (tmp_path / "staged" / "same-secret.yaml").write_text(
        "kind: Secret # same-secret.yaml v2\n"
    )
    (tmp_path / "staged" / "edited-secret.yaml").write_text(
        "kind: Secret # edited-secret.yaml v1\n"
    )
    # The render writes 0600, so a staged file at the umask's mode would read as changed.
    for staged in (tmp_path / "staged").iterdir():
        staged.chmod(0o600)
    render = {
        "name": "Render the fixture Secrets",
        "no_log": RENDER["no_log"],
        "ansible.builtin.template": {
            "src": "{{ playbook_dir }}/templates/{{ item }}.j2",
            "dest": "{{ playbook_dir }}/staged/{{ item }}",
            "mode": "0600",
        },
        "loop": "{{ manifests_secret_files_resolved }}",
        "register": RENDER["register"],
    }
    record = {
        "name": "Record the fact and the register",
        "ansible.builtin.copy": {
            "dest": "{{ playbook_dir }}/result.json",
            "content": (
                "{{ {'files': observability_changed_secret_files,"
                " 'results': manifests_secret_render.results} | to_json }}"
            ),
        },
    }
    strip = {"tags"}
    play = [
        {
            "hosts": "all",
            "gather_facts": False,
            "vars": {"manifests_secret_files_resolved": files},
            "tasks": [
                render,
                {k: v for k, v in CHECK.items() if k not in strip},
                {k: v for k, v in CHANGED_FILES.items() if k not in strip},
                record,
            ],
        }
    ]
    (tmp_path / "play.yml").write_text(yaml.safe_dump(play, sort_keys=False))
    (tmp_path / "hosts.yml").write_text(
        yaml.safe_dump({"all": {"hosts": {"fixture": {"ansible_connection": "local"}}}})
    )
    env = scrubbed_env()
    env["ANSIBLE_PYTHON_INTERPRETER"] = sys.executable
    env["ANSIBLE_CONFIG"] = str(tmp_path / "none.cfg")
    env["ANSIBLE_NOCOLOR"] = "1"
    result = subprocess.run(
        ["ansible-playbook", "play.yml", "-i", "hosts.yml"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    recorded = json.loads((tmp_path / "result.json").read_text())
    assert [r.get("changed") for r in recorded["results"]] == [False, True, True], (
        recorded["results"]
    )
    assert recorded["files"] == ["edited-secret.yaml", "new-secret.yaml"]


def test_the_check_accepts_a_register_that_names_every_file() -> None:
    files = ["a-secret.yaml", "b-secret.yaml"]
    results = [{"changed": False}, {"changed": True}]
    assert _check_passes(files, results)
    assert _changed_files(files, results) == ["b-secret.yaml"]


@pytest.mark.parametrize(
    "results",
    [
        pytest.param([{"changed": False}, {"censored": "hidden"}], id="no-changed"),
        pytest.param([{"changed": True}], id="one-result-short"),
    ],
)
def test_the_check_refuses_a_register_it_cannot_pair(results: list[dict]) -> None:
    assert not _check_passes(["a-secret.yaml", "b-secret.yaml"], results)


def _restarted(changed: list[str]) -> set[str]:
    """The workloads the private restart runs for, with nothing created or rolled by the apply.

    The `created` clause is left out: its `\\.apps/` escape is accepted by Ansible's templar and
    rejected by plain Jinja. test_self_rollouts_follow_the_apply.py pins that clause.
    """
    clauses = [c for c in RESTART["when"] if " created" not in c]
    return {
        item["name"]
        for item in WORKLOADS
        if all(
            render_expr(
                "{{ " + clause + " }}",
                item=item,
                k8s_no_mutate=False,
                observability_changed_secret_files=changed,
                manifests_rolled_by_apply={},
            )
            for clause in clauses
        )
    }


def test_an_alertmanager_config_edit_restarts_alertmanager_alone() -> None:
    assert _restarted(["alertmanager-secret.yaml"]) == {"alertmanager"}


def test_a_scrape_credentials_edit_restarts_prometheus_alone() -> None:
    assert _restarted(["scrape-credentials-secret.yaml"]) == {"prometheus"}


def test_an_origin_pull_edit_restarts_nothing() -> None:
    """No pod reads `origin-pull-secret.yaml`; a Traefik middleware does."""
    assert _restarted(["origin-pull-secret.yaml"]) == set()


def _snapshotted(changed: list[str]) -> set[str]:
    """The workloads the snapshot hands to the play gate after an apply that moved nothing."""
    inert = {"changed": False, "stdout": "deployment.apps/grafana unchanged"}
    return {
        item["name"]
        for item in WORKLOADS
        if all(
            render_expr(
                "{{ " + " ".join(clause.split()) + " }}",
                item=item,
                k8s_no_mutate=False,
                manifests_render={"changed": False},
                manifests_apply=inert,
                observability_changed_secret_files=changed,
            )
            for clause in SNAPSHOT["when"]
        )
    }


def test_an_alertmanager_config_edit_soaks_alertmanager_alone() -> None:
    assert _snapshotted(["alertmanager-secret.yaml"]) == {"alertmanager"}


def test_a_grafana_password_rotation_soaks_grafana_alone() -> None:
    assert _snapshotted(["grafana-secret.yaml"]) == {"grafana"}


def test_a_grafana_password_rotation_restarts_grafana_alone() -> None:
    assert _restarted(["grafana-secret.yaml"]) == {"grafana"}


def test_grafana_restarts_on_the_one_secret_file_that_renders_the_admin_password() -> (
    None
):
    """A rename of the template, or a second Secret carrying the password, would leave grafana
    restarting on a file that never changes for a rotation, and the initContainer would never
    rerun. Rendered with a sentinel, so the census finds the password's value however a
    template reaches it."""
    sentinel = "sentinel-grafana-admin-password"
    templates = sorted((OBSERVABILITY / "templates").glob("*secret*.yaml.j2"))
    assert len(templates) >= 3, templates
    rendering = [
        template.name.removesuffix(".j2")
        for template in templates
        if sentinel
        in render_role_template(
            "observability", template.name, {"grafana_admin_password": sentinel}
        )
    ]
    assert rendering == ["grafana-secret.yaml"]
    grafana = next(w for w in WORKLOADS if w["name"] == "grafana")
    assert rendering[0] in grafana["restart_secrets"]


def _grafana_pod() -> dict:
    docs = yaml_fast.safe_load_all(
        render_role_template("observability", "grafana.yaml.j2")
    )
    return pod_spec(
        next(
            d
            for d in docs
            if d and d["kind"] == "Deployment" and d["metadata"]["name"] == "grafana"
        )
    )


def test_grafana_resets_the_admin_password_from_the_secret_before_it_starts() -> None:
    """GF_SECURITY_ADMIN_PASSWORD seeds only a new database, so a rotation needs the CLI.

    The initContainer reads the password from the Secret the server reads, on stdin, so the
    value never lands in the pod spec or the process list, and it writes the same volume the
    server mounts as its data directory.
    """
    pod = _grafana_pod()
    server = next(c for c in pod["containers"] if c["name"] == "grafana")
    [init] = [
        c
        for c in pod.get("initContainers") or []
        if "reset-admin-password" in " ".join(c["command"])
    ]
    assert init["image"] == server["image"]
    script = init["command"][-1]
    assert "--password-from-stdin" in script
    assert '"$GF_SECURITY_ADMIN_PASSWORD"' in script
    [password] = [e for e in init["env"] if e["name"] == "GF_SECURITY_ADMIN_PASSWORD"]
    assert password["valueFrom"]["secretKeyRef"] == {
        "name": "grafana-admin",
        "key": "password",
    }
    data = next(m for m in server["volumeMounts"] if m["name"] == "grafana-data")
    assert {"name": "grafana-data", "mountPath": data["mountPath"]} in init[
        "volumeMounts"
    ]


def test_the_role_runs_no_grafana_cli_of_its_own() -> None:
    """The initContainer replaced the task that exec'd the CLI into the running server, where
    its own migration pass contended with the server's for the SQLite file."""
    commands = [str(t) for t in walk_tasks(TASKS)]
    assert not [
        c for c in commands if "reset-admin-password" in c or "exec deploy/grafana" in c
    ]


def test_the_amend_raises_only_the_workloads_the_restart_ran_for() -> None:
    """The amend loops the restart's own register, minus its skip results, under a loop
    variable of its own. rollout_amend.yml loops with `item` itself, and the amend's vars are
    templated lazily, so an `item` here would resolve against that inner loop's entry."""
    assert AMEND["loop"].startswith("{{ " + RESTART["register"] + ".results")
    loop_var = AMEND["loop_control"]["loop_var"]
    assert loop_var != "item"
    assert loop_var in AMEND["vars"]["manifests_amend_workload"]
    assert (
        AMEND["vars"]["manifests_amend_restart"] == f"{{{{ {loop_var} is changed }}}}"
    )
    register = {
        "results": [
            {"changed": True, "item": {"name": "alertmanager"}},
            {"changed": False, "skipped": True, "item": {"name": "prometheus"}},
        ]
    }
    looped = render_expr(AMEND["loop"], **{RESTART["register"]: register})
    assert [r["item"]["name"] for r in looped] == ["alertmanager"]


def _held_to_a_restart(changed: list[str]) -> set[str]:
    """The workloads the release record holds to a restart once the role has run.

    The shared stamp writes the record, the private restart picks its workloads, and the amend
    rewrites the record once per workload it restarted, as the play does.
    """
    record = list(
        stamped_rollouts(
            manifests_service="observability",
            manifests_rollout="",
            manifests_extra_rollouts=[],
            manifests_self_rollouts=observability_self_rollouts(),
            manifests_render={"changed": False},
            manifests_secret_render={"changed": True},
            manifests_apply={"stdout": "deployment.apps/alertmanager unchanged"},
        ).values()
    )
    for workload in sorted(_restarted(changed)):
        amended: list[dict] = []
        for entry in record:
            amended = render_expr(
                AMEND_REWRITE["ansible.builtin.set_fact"]["manifests_amend_rollouts"],
                manifests_amend_rollouts=amended,
                item=entry,
                manifests_amend_workload=workload,
                manifests_amend_restart=True,
            )
        record = amended
    return {entry["name"] for entry in record if entry["restart"]}


def test_an_alertmanager_config_edit_holds_alertmanager_alone_to_a_restart() -> None:
    """M1's exit criterion 4, as `probe.py releases` reads it after the deploy."""
    assert _held_to_a_restart(["alertmanager-secret.yaml"]) == {"alertmanager"}


def test_a_grafana_password_rotation_holds_grafana_alone_to_a_restart() -> None:
    assert _held_to_a_restart(["grafana-secret.yaml"]) == {"grafana"}


def test_the_amend_runs_after_the_rollout_wait() -> None:
    names = [str(t.get("name", "")) for t in walk_tasks(TASKS)]
    restart = names.index(RESTART["name"])
    wait = names.index("Wait for the telemetry workloads to roll out")
    amend = names.index(AMEND["name"])
    assert restart < wait < amend, names


def _unfingerprinted(self_rollouts: list[dict]) -> set[str]:
    """The Secret readers the shared role's fingerprint list leaves out."""
    targets = task_named(MAIN, "List the workloads whose pod template is fingerprinted")
    fingerprinted = render_expr(
        targets["ansible.builtin.set_fact"]["manifests_fingerprint_targets"],
        manifests_service="observability",
        **{
            k: v
            for k, v in include_role_vars(OBSERVABILITY).items()
            if k == "manifests_rollout"
        },
        manifests_rollout_kind="deploy",
        manifests_extra_rollouts=[],
        manifests_self_rollouts=self_rollouts,
    )
    return set(SECRET_READERS) - {e["name"] for e in fingerprinted}


def test_every_secret_reader_is_fingerprinted_across_the_apply_is_clean() -> None:
    assert _unfingerprinted(observability_self_rollouts()) == set()


def test_a_secret_reader_marked_rolled_by_role_is_flagged() -> None:
    """`rolled_by_role` drops the entry from the fingerprints, so the restart's skip reads
    "not rolled" on an image bump and rolls the Recreate pod a second time (#3115)."""
    marked = [
        {**e, "rolled_by_role": True} if e["name"] == "alertmanager" else e
        for e in observability_self_rollouts()
    ]
    assert _unfingerprinted(marked) == {"alertmanager"}
