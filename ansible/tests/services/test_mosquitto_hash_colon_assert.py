"""mosquitto's deploy refuses a stored password hash that carries a `user:` prefix.

templates/config-secret.yaml.j2 renders `mqtt_username:mqtt_password_hash` with no transform on
the hash (R2's PR 4), so a stored `mosquitto_passwd` line would render `user:user:$7$...`. Two
tasks in roles/k8s/mosquitto/tasks/main.yml guard it before the manifests include: one sets
the colon count as a fact under `no_log`, and an assert fails unless the count is 0. These
tests evaluate both tasks' real expressions through Ansible's own templar.
"""

import pytest
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template
from lib import yaml_fast

from _helpers import ROLES

_TASKS = ROLES / "k8s" / "mosquitto" / "tasks" / "main.yml"
_COUNT = "Count the colons in the stored mosquitto password hash"
_ASSERT = "Fail if the stored mosquitto password hash carries a user prefix"
_INCLUDE = "Deploy mosquitto to the cluster"


def _tasks() -> list[dict]:
    return yaml_fast.safe_load(_TASKS.read_text())


def _index(name: str) -> int:
    found = [i for i, t in enumerate(_tasks()) if t.get("name") == name]
    # Non-vacuity: every test reads these tasks, so an absent one must fail by name.
    assert len(found) == 1, (
        f"expected one task named {name!r} in {_TASKS}, found {len(found)}"
    )
    return found[0]


def _count_and_pass(stored: str) -> tuple[int, bool]:
    """The fact the count task sets for `stored`, and whether the assert passes on it."""
    tasks = _tasks()
    ((fact, expr),) = tasks[_index(_COUNT)]["ansible.builtin.set_fact"].items()
    templar = Templar(loader=DataLoader(), variables={"mqtt_password_hash": stored})
    count = templar.template(trust_as_template(expr))
    templar = Templar(loader=DataLoader(), variables={fact: count})
    that = tasks[_index(_ASSERT)]["ansible.builtin.assert"]["that"]
    clauses = that if isinstance(that, list) else [that]
    passed = all(
        templar.template(trust_as_template("{{ " + clause + " }}"))
        for clause in clauses
    )
    return int(count), passed


def test_a_bare_hash_counts_zero_and_passes():
    assert _count_and_pass("$7$abc") == (0, True)


@pytest.mark.parametrize(
    "stored, colons",
    [
        pytest.param("user:$7$abc", 1, id="user-prefix"),
        pytest.param("a:user:$7$abc", 2, id="double-prefix"),
    ],
)
def test_a_prefixed_hash_fails(stored, colons):
    assert _count_and_pass(stored) == (colons, False)


def test_both_tasks_precede_the_manifests_include():
    include = _index(_INCLUDE)
    assert _tasks()[include]["ansible.builtin.include_role"]["name"] == "k8s/manifests"
    assert _index(_COUNT) < _index(_ASSERT) < include


def test_only_the_count_task_is_no_log():
    """The count hides the value; the assert stays loggable, so its message shows the count."""
    tasks = _tasks()
    assert tasks[_index(_COUNT)].get("no_log") is True
    assert not tasks[_index(_ASSERT)].get("no_log")
    assert (
        "mqtt_password_hash }}"
        not in tasks[_index(_ASSERT)]["ansible.builtin.assert"]["fail_msg"]
    )
