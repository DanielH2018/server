"""Guards `k8s_retired_services`, the list whose live objects every k8s deploy tears down.

ansible/tasks/k8s_retire.yml deletes every object a listed service's staged manifests
declare, its PersistentVolumeClaims and their Longhorn volumes included. A name that is
still a live service would have its data deleted by the same play that deploys it, so the
list may only name a service that has neither a role nor a containers_list entry. The play
asserts the containers_list half at run time; these tests catch both halves at review time.

Run: uv run pytest ansible/tests/deploy/test_k8s_retired_services.py
"""

from lib import yaml_fast

from _helpers import (
    ALL_VARS,
    ANSIBLE,
    HOST_VARS,
    command_of,
    load_tasks,
    task_named,
)
from _role_census import role_dirs

_RETIRE_TASKS = ANSIBLE / "tasks" / "k8s_retire.yml"


def _retired_names() -> list[str]:
    entries = (yaml_fast.safe_load(ALL_VARS.read_text()) or {}).get(
        "k8s_retired_services"
    ) or []
    return [e["name"] for e in entries]


def _deployed_names() -> set[str]:
    names = {p.name for p in role_dirs()}
    for path in HOST_VARS.glob("*.yml"):
        doc = yaml_fast.safe_load(path.read_text()) or {}
        names.update(c["name"] for c in doc.get("containers_list") or [])
    return names


def _still_deployed(retired: list[str], deployed: set[str]) -> list[str]:
    return [name for name in retired if name in deployed]


def test_no_retired_service_is_still_a_role_or_a_containers_list_entry():
    retired = _retired_names()
    deployed = _deployed_names()
    # The named member: the list's first entry, so the check cannot pass on an empty read.
    assert "healthchecks" in retired
    # The proof it can go red: a live service fed in is reported.
    assert _still_deployed(["sonarr"], deployed) == ["sonarr"]
    assert _still_deployed(retired, deployed) == []


def test_the_delete_survives_a_rerun_and_holds_the_directory_until_objects_are_gone():
    cmd = command_of(task_named(load_tasks(_RETIRE_TASKS), "Delete every object"))
    assert "--ignore-not-found" in cmd
    assert "--wait=true" in cmd
