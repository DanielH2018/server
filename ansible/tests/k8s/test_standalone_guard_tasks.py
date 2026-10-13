"""The manifests role refuses a standalone secret value Flux would retype (target slice R2).

The role's two guard tasks run verbatim in a real `ansible-playbook` against a fixture map
under a fixture `playbook_dir`, so ansible-core's own templar and the real filter plugin
decide. Every value is a fixture string, never a secret.

Run: uv run pytest ansible/tests/k8s/test_standalone_guard_tasks.py
"""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from lib.git_testing import scrubbed_env

from _helpers import ANSIBLE, load_tasks

MANIFESTS = ANSIBLE / "roles" / "k8s" / "manifests" / "tasks" / "main.yml"
FIND = "Find the standalone secret values Flux would retype"
REFUSE = "Refuse a standalone secret value that Flux would retype"
_MAP = {"widget": ["alpha_token", "domain"]}


def _index(fragment: str) -> int:
    for index, task in enumerate(load_tasks(MANIFESTS)):
        if str(task.get("name", "")).startswith(fragment):
            return index
    raise AssertionError(
        f"no task named {fragment!r} in {MANIFESTS}. If it was renamed, update this test "
        f"rather than deleting it."
    )


def _task(fragment: str) -> dict:
    return load_tasks(MANIFESTS)[_index(fragment)]


def _run(tmp_path: Path, hosts: dict[str, dict], guard_map=_MAP) -> dict[str, dict]:
    """Run the two tasks once per fixture host and return what each host recorded."""
    files = tmp_path / "roles" / "k8s" / "manifests" / "files"
    files.mkdir(parents=True)
    if guard_map is not None:
        (files / "standalone_secret_keys.json").write_text(json.dumps(guard_map))
    (tmp_path / "results").mkdir()
    find = {**_task(FIND), "register": "find_result", "ignore_errors": True}
    refuse = {**_task(REFUSE), "register": "refuse_result", "ignore_errors": True}
    record = {
        "name": "Record what the tasks decided",
        "ansible.builtin.copy": {
            "dest": "{{ playbook_dir }}/results/{{ inventory_hostname }}.json",
            "content": (
                "{{ {'find_failed': find_result.failed | default(false),"
                " 'find_msg': find_result.msg | default(''),"
                " 'refused': manifests_standalone_refused | default(None),"
                " 'refuse': refuse_result} | to_json }}"
            ),
        },
    }
    play = [{"hosts": "all", "gather_facts": False, "tasks": [find, refuse, record]}]
    (tmp_path / "play.yml").write_text(yaml.safe_dump(play, sort_keys=False))
    base = {"ansible_connection": "local", "manifests_service": "widget"}
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


pytestmark = pytest.mark.skipif(
    shutil.which("ansible-playbook") is None, reason="ansible-playbook not on PATH"
)


def test_a_mapped_value_flux_would_retype_is_flagged(tmp_path: Path) -> None:
    results = _run(
        tmp_path,
        {
            "digits": {"alpha_token": "0123", "domain": "example.org"},
            "undefined": {"domain": "example.org"},
            "int_from_sops": {"alpha_token": 123, "domain": "example.org"},
        },
    )
    assert {host: r["refused"] for host, r in results.items()} == {
        "digits": ["alpha_token"],
        "undefined": ["alpha_token"],
        "int_from_sops": ["alpha_token"],
    }
    for host, recorded in results.items():
        message = recorded["refuse"]["msg"]
        assert recorded["refuse"]["failed"], host
        assert "widget" in message and "alpha_token" in message, message
        assert not re.search(r"0123|example\.org", message), (
            f"{host}: the refusal printed a value: {message}"
        )


def test_a_mapped_value_flux_keeps_is_clean(tmp_path: Path) -> None:
    results = _run(
        tmp_path,
        {
            "strings": {"alpha_token": "a1b2c3", "domain": "example.org"},
            # A tag the map does not name has no standalone key, and passes whatever it holds.
            "unmapped": {"manifests_service": "gadget", "alpha_token": "0123"},
        },
    )
    for host, recorded in results.items():
        assert recorded["refused"] == [], (host, recorded)
        assert not recorded["refuse"].get("failed"), (host, recorded["refuse"])


def test_a_missing_map_fails_the_first_task(tmp_path: Path) -> None:
    (recorded,) = _run(
        tmp_path, {"no_map": {"domain": "example.org"}}, guard_map=None
    ).values()
    assert recorded["find_failed"]


def test_both_tasks_precede_every_render_and_only_the_first_reads_values() -> None:
    tasks = load_tasks(MANIFESTS)
    find, refuse = _index(FIND), _index(REFUSE)
    renders = [
        i for i, t in enumerate(tasks) if str(t.get("name", "")).startswith("Render ")
    ]
    assert renders, "no task named `Render ...` in the manifests role"
    assert find < refuse < min(renders)
    assert tasks[find].get("no_log") is True
    assert "no_log" not in tasks[refuse]
    for index in (find, refuse):
        assert tasks[index].get("tags") == ["always"]
    # The message may template only the service name and the refused key names.
    message = tasks[refuse]["ansible.builtin.assert"]["fail_msg"]
    names = set(re.findall(r"\{\{\s*([a-z_]+)", message))
    assert names == {"manifests_service", "manifests_standalone_refused"}, names
