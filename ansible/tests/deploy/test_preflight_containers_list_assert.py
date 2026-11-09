"""`preflight.yml` asks for `containers_list` on a Docker host alone.

The k3s services live in `cluster/services.yml`, so daniel-box and daniel-server declare no
`containers_list` and must pass. A Docker host still needs the key, because `deploy.yml` loops
over it. Each expression is evaluated with Ansible's own Templar against the real host_vars.

Run: uv run pytest ansible/tests/deploy/test_preflight_containers_list_assert.py
"""

from lib import yaml_fast
from lib.repo_paths import ALL_VARS, HOST_VARS

from _helpers import ANSIBLE

TASK = "A Docker host declares containers_list"


def _task() -> dict:
    (play,) = yaml_fast.safe_load((ANSIBLE / "preflight.yml").read_text())
    (task,) = [t for t in play["tasks"] if t.get("name") == TASK]
    return task


def _fails(host_vars: dict) -> bool:
    """Whether the task runs on a host with `host_vars` and its assert is false there."""
    from ansible.parsing.dataloader import DataLoader
    from ansible.template import Templar, trust_as_template

    task = _task()
    variables = {**yaml_fast.safe_load(ALL_VARS.read_text()), **host_vars}
    templar = Templar(loader=DataLoader(), variables=variables)

    def true(expr: str) -> bool:
        return templar.template(trust_as_template("{{ " + expr + " }}")) is True

    return true(task["when"]) and not true(task["ansible.builtin.assert"]["that"])


def test_the_k3s_nodes_without_containers_list_are_clean():
    for host in ("daniel-box", "daniel-server"):
        host_vars = yaml_fast.safe_load((HOST_VARS / f"{host}.yml").read_text())
        assert "containers_list" not in host_vars
        assert not _fails(host_vars), host


def test_a_docker_host_without_containers_list_is_flagged():
    pi = yaml_fast.safe_load((HOST_VARS / "daniel-pi.yml").read_text())
    assert not _fails(pi)
    assert _fails({k: v for k, v in pi.items() if k != "containers_list"})
