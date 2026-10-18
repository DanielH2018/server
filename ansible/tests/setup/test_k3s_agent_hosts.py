"""Every host in `k3s_agent_hosts` is one `k3s-bringup.yml`'s join play admits.

The deployer prints `-e join_agent=<host> --tags k3s_agent` for each listed host (#5606). A
host the join play's assert refuses would make that printed command fail, and a join play
without the `k3s_agent` tag would make it exit 0 having applied nothing.
"""

from lib import yaml_fast

from _helpers import ANSIBLE
from lib.repo_paths import ALL_VARS

BRINGUP = ANSIBLE / "k3s-bringup.yml"


def _join_play() -> dict:
    """The play whose hosts ARE `join_agent`; its two neighbours run on the server node."""
    plays = yaml_fast.safe_load(BRINGUP.read_text())
    (play,) = [p for p in plays if p.get("hosts") == "{{ join_agent | default([]) }}"]
    return play


def _admits(that: str, host: str) -> bool:
    return f"'{host}'" in that or f'"{host}"' in that


def test_every_agent_host_is_admitted_by_the_join_assert():
    hosts = yaml_fast.safe_load(ALL_VARS.read_text())["k3s_agent_hosts"]
    assert hosts
    (that,) = [
        str(t["ansible.builtin.assert"]["that"])
        for t in _join_play()["pre_tasks"]
        if "ansible.builtin.assert" in t
    ]
    assert all(_admits(that, h) for h in hosts), (that, hosts)
    assert not _admits(that, "daniel-box"), "the admit check matches any quoted name"


def test_the_join_play_applies_agent_yml_under_the_printed_tag():
    (task,) = [t for t in _join_play()["tasks"] if "ansible.builtin.include_role" in t]
    assert task["ansible.builtin.include_role"]["tasks_from"] == "agent"
    assert "k3s_agent" in task["tags"]
