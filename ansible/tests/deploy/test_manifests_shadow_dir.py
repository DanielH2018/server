"""The manifests role keeps a stubbed dry-run render only under `manifests_shadow_dir`.

The variable is the Ansible half of the shadow render (target slice R1). Set, it keeps each
service's render in `<dir>/<service>` instead of a discarded temporary directory, so the tree
must hold stub values only. The role's first task refuses it outside a k8s dry run and while any
secret key holds a value other than its stub. That assert runs after the SOPS load, so it
compares the values about to be rendered, and no flag or sentinel may stand in for it.

These tests copy the role's own tasks verbatim, with `become` dropped: the assert, the temporary
directory, the directory choice and its discard. They run in a real `ansible-playbook` against a
fixture `vars/secrets.yml`, so ansible-core's own conditional rules decide. Each fixture host is
one case, and every refusal has an accepted twin.
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
from secret_stubs import homelab_secret_stub

from _helpers import ANSIBLE, load_tasks

MANIFESTS = ANSIBLE / "roles" / "k8s" / "manifests" / "tasks" / "main.yml"

REFUSE = "Refuse a shadow render directory outside a stubbed dry run"
TEMPFILE = "Create a throwaway render directory"
SELECT = "Select the render directory"
DISCARD = "Discard the throwaway render directory"

_SECRETS = """\
alpha_token: ENC[AES256_GCM,data:AAAA,type:str]
domain: ENC[AES256_GCM,data:BBBB,type:str]
become_password: ENC[AES256_GCM,data:CCCC,type:str]
sops:
    version: 3.9.0
"""
_KEYS = ("alpha_token", "domain")
_STUBS = {key: homelab_secret_stub(key) for key in _KEYS}
# What the k8s play holds after the SOPS load on a run with no stub file.
_REAL = {key: f"real-value-of-{key}" for key in _KEYS}
_SHADOW = "/var/tmp/homelab-shadow/X/ansible"


def _named(fragment: str) -> tuple[int, dict]:
    for index, task in enumerate(load_tasks(MANIFESTS)):
        if str(task.get("name", "")).startswith(fragment):
            return index, task
    raise AssertionError(
        f"no task named {fragment!r} in {MANIFESTS}. If it was renamed, update this test "
        f"rather than deleting it."
    )


def _run(tmp_path: Path, hosts: dict[str, dict]) -> dict[str, dict]:
    """Run the four tasks once per fixture host and return what each host recorded.

    `playbook_dir` is `tmp_path`, so the assert reads the fixture `vars/secrets.yml`.
    """
    (tmp_path / "vars").mkdir()
    (tmp_path / "vars" / "secrets.yml").write_text(_SECRETS)
    (tmp_path / "results").mkdir()
    tasks = []
    for fragment in (REFUSE, TEMPFILE, SELECT, DISCARD):
        task = {k: v for k, v in _named(fragment)[1].items() if k != "become"}
        if fragment == REFUSE:
            task = {**task, "register": "refuse_result", "ignore_errors": True}
        tasks.append(task)
    record = {
        "name": "Record what the tasks decided",
        "ansible.builtin.copy": {
            "dest": "{{ playbook_dir }}/results/{{ inventory_hostname }}.json",
            "content": (
                "{{ {'refuse': refuse_result, 'dest': manifests_dest_dir,"
                " 'owner': manifests_dest_owner,"
                " 'tempdir': manifests_dry_run_dir.path | default('')} | to_json }}"
            ),
        },
    }
    play = [{"hosts": "all", "gather_facts": False, "tasks": [*tasks, record]}]
    (tmp_path / "play.yml").write_text(yaml.safe_dump(play, sort_keys=False))
    base = {
        "ansible_connection": "local",
        "manifests_service": "widget",
        "sys_user": "shadow-owner",
        "manifests_shadow_dir": "",
        "k8s_dry_run": True,
    }
    inventory = {"all": {"hosts": {name: {**base, **hv} for name, hv in hosts.items()}}}
    (tmp_path / "hosts.yml").write_text(yaml.safe_dump(inventory))

    env = scrubbed_env()
    env["ANSIBLE_FILTER_PLUGINS"] = str(ANSIBLE / "filter_plugins")
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
    if result.returncode != 0:
        pytest.fail(
            f"ansible-playbook exited {result.returncode}.\n{result.stdout}\n{result.stderr}"
        )
    return {
        name: json.loads((tmp_path / "results" / f"{name}.json").read_text())
        for name in hosts
    }


def _verdict(recorded: dict) -> str:
    refuse = recorded["refuse"]
    if refuse.get("skipped"):
        return "skipped"
    return "refused" if refuse.get("failed") else "passed"


pytestmark = pytest.mark.skipif(
    shutil.which("ansible-playbook") is None, reason="ansible-playbook not on PATH"
)


def test_the_assert_keeps_a_shadow_render_to_a_fully_stubbed_dry_run(
    tmp_path: Path,
) -> None:
    hosts = {
        "shadow_dry_run": {**_STUBS, "manifests_shadow_dir": _SHADOW},
        # An ordinary dry run and a real deploy never read the secrets for the assert.
        "plain_dry_run": _REAL,
        "real_deploy": {**_REAL, "k8s_dry_run": False},
        # `deploy.sh --dry-run -e manifests_shadow_dir=...` with no stub file.
        "no_stub_file": {**_REAL, "manifests_shadow_dir": _SHADOW},
        # The case an assert that read a flag or the sentinel would pass.
        "flag_and_sentinel": {
            **_REAL,
            "homelab_secret_stubs": True,
            "homelab_secret_stubs_verified": True,
            "manifests_shadow_dir": _SHADOW,
        },
        "one_key_real": {
            **_STUBS,
            "alpha_token": "real-value-of-alpha_token",
            "manifests_shadow_dir": _SHADOW,
        },
        "real_run_with_stubs": {
            **_STUBS,
            "k8s_dry_run": False,
            "manifests_shadow_dir": _SHADOW,
        },
    }
    results = _run(tmp_path, hosts)
    assert {host: _verdict(r) for host, r in results.items()} == {
        "shadow_dry_run": "passed",
        "plain_dry_run": "skipped",
        "real_deploy": "skipped",
        "no_stub_file": "refused",
        "flag_and_sentinel": "refused",
        "one_key_real": "refused",
        "real_run_with_stubs": "refused",
    }
    one_key = results["one_key_real"]["refuse"]["msg"]
    assert "alpha_token" in one_key and "domain" not in one_key, one_key
    for host in ("no_stub_file", "flag_and_sentinel", "one_key_real"):
        message = results[host]["refuse"]["msg"]
        assert "real-value-of" not in message, (
            f"{host}: the refusal printed a secret value: {message}"
        )
    assert "become_password" not in results["no_stub_file"]["refuse"]["msg"]
    assert "not a k8s dry run" in results["real_run_with_stubs"]["refuse"]["msg"]


def test_only_a_shadow_dry_run_keeps_its_render(tmp_path: Path) -> None:
    """Unset, the dry run renders to a temp dir it discards; set, to a kept `sys_user` tree."""
    results = _run(
        tmp_path,
        {
            "shadow_dry_run": {**_STUBS, "manifests_shadow_dir": _SHADOW},
            "plain_dry_run": _REAL,
            "real_deploy": {**_REAL, "k8s_dry_run": False},
            "real_run_with_stubs": {
                **_STUBS,
                "k8s_dry_run": False,
                "manifests_shadow_dir": _SHADOW,
            },
        },
    )
    shadow = results["shadow_dry_run"]
    assert (shadow["dest"], shadow["owner"], shadow["tempdir"]) == (
        f"{_SHADOW}/widget",
        "shadow-owner",
        "",
    ), "a shadow render created a temp dir, or renders somewhere else"

    plain = results["plain_dry_run"]
    assert plain["owner"] == "root"
    assert plain["tempdir"] and plain["dest"] == plain["tempdir"]
    assert plain["dest"].endswith("-widget-dryrun")
    assert not Path(plain["tempdir"]).exists(), "the ordinary dry run kept its temp dir"

    for host in ("real_deploy", "real_run_with_stubs"):
        assert (results[host]["dest"], results[host]["owner"]) == (
            "/etc/rancher/k3s/manifests/widget",
            "root",
        ), f"{host}: a real run's staging directory moved or changed owner"


def test_the_assert_runs_first_whatever_the_tags() -> None:
    index, task = _named(REFUSE)
    assert task.get("tags") == ["always"], f"{REFUSE!r} is skipped by a --tags run"
    assert str(task.get("when")) == "manifests_shadow_dir | length > 0", (
        "the assert's `when` must read the variable alone. A `k8s_dry_run` there skips the "
        "refusal on a real run instead of failing it."
    )
    assert "k8s_dry_run | bool" in task["ansible.builtin.assert"]["that"]
    writers = [
        _named(name)[0]
        for name in (TEMPFILE, "Create the manifest directory", "Render manifests")
    ]
    assert index < min(writers), f"{REFUSE!r} runs after a task that writes the render"


def test_every_render_directory_write_takes_the_one_owner_fact() -> None:
    """A task left at `owner: root` leaves a file in the shadow tree the operator cannot read."""
    writers = [
        task
        for task in yaml_fast.safe_load(MANIFESTS.read_text())
        if "manifests_d"
        in str(task.get("ansible.builtin.file", {}).get("path", ""))
        + str(task.get("ansible.builtin.template", {}).get("dest", ""))
        and task.get("ansible.builtin.file", {}).get("state") != "absent"
    ]
    names = {str(t["name"]).split(" for ")[0] for t in writers}
    assert {
        "Create the manifest directory",
        "Render secret manifests",
        "Render deferred manifests",
    } <= names, f"the census of render-directory writes lost a member: {sorted(names)}"
    for task in writers:
        module = task.get("ansible.builtin.file") or task["ansible.builtin.template"]
        assert (
            module.get("owner") == module.get("group") == "{{ manifests_dest_owner }}"
        ), f"{task['name']!r} sets its own owner"
