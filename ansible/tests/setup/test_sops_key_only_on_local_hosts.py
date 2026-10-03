"""Only a host Ansible drives locally gets a SOPS age key from `sops_setup`.

`pre_tasks/load_secrets.yml` decrypts on the controller, so a host driven over ssh never
decrypts and a key there only widens who can read `ansible/vars/secrets.yml`. daniel-pi held
one until 2026-10-03 without any job using it.

The gate is evaluated per inventory host rather than matched as text, and the test needs both
outcomes to appear, so a gate that is always true (or missing) and one that is always false
both fail.

Run: uv run pytest ansible/tests/setup/test_sops_key_only_on_local_hosts.py
"""

from _helpers import ANSIBLE
from lib import yaml_fast
from lib.ansible_inventory import inventory_hosts
from lib.ansible_jinja_env import make_ansible_env

TASKS = ANSIBLE / "roles" / "setup" / "sops_setup" / "tasks" / "main.yml"

# Every task that creates or reads the host's private key file. Named rather than counted,
# so a rename fails with the member that went missing.
KEY_TASKS = frozenset(
    {
        "Generate Age key (only if it doesn't exist)",
        "Display the new Public Key",
        "Show the Public Key to user",
    }
)


def _key_task_gates() -> dict[str, str | None]:
    tasks = yaml_fast.safe_load(TASKS.read_text())
    return {t["name"]: t.get("when") for t in tasks if t.get("name") in KEY_TASKS}


def test_every_key_task_gets_a_key_only_on_a_locally_driven_host():
    gates = _key_task_gates()
    assert set(gates) == KEY_TASKS, (
        f"key tasks missing from {TASKS}: {KEY_TASKS - set(gates)}"
    )
    env = make_ansible_env()
    outcomes = set()
    for host in inventory_hosts():
        for name, when in gates.items():
            assert when is not None, (
                f"{name!r} has no `when:`, so every host gets a key"
            )
            runs = bool(
                env.compile_expression(when)(ansible_connection=host.connection)
            )
            assert runs == (host.connection == "local"), (
                f"{name!r} on {host.name} (ansible_connection={host.connection}) "
                f"evaluates to {runs}"
            )
            outcomes.add(runs)
    assert outcomes == {True, False}, (
        "the inventory must hold both a local and an ssh host"
    )
