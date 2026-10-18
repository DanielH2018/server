#!/usr/bin/env python3
"""The k8s play skips a containers_list entry that carries `owner`, and only that entry.

`ansible_owned` in filter_plugins/toposort.py keeps the entries without `owner`, and deploy.yml
batches `containers_list | ansible_owned`. The containers_list fact itself keeps every entry,
because templates read it. The plan is docs/target-architecture/deploy/d2-first-leaf.md, D2.1d.

The play tests evaluate deploy.yml's own expressions through ansible's Templar, with the filters
registered through `FilterModule().filters()` as Ansible loads them, so moving the filter into a
set_fact or dropping its registration fails here.

Run: uv run pytest ansible/tests/deploy/test_ansible_owned_filter.py
"""

from typing import Any

import pytest
from ansible.errors import AnsibleFilterError
from lib import yaml_fast
from lib.k8s_roles import OWNER_VALUES as SCRIPT_OWNER_VALUES

import toposort
from _helpers import ANSIBLE, HOST_VARS
from toposort import OWNER_VALUES, ansible_owned

DEPLOY = ANSIBLE / "deploy.yml"

# The k8s play's tasks that set the containers_list fact, in play order, then the batch loop.
_FACT_TASKS = (
    "Restrict this play to k8s-platform containers",
    "Build k8s role dependency map",
    "Sort k8s containers by dependency order",
)
_BATCH_TASK = "Deploy k8s workloads in batches"


def _names(containers):
    return [c["name"] for c in containers]


def _host_var_files():
    return sorted(p for p in HOST_VARS.glob("*.yml") if not p.name.startswith("_"))


def _containers(path):
    return (yaml_fast.safe_load(path.read_text()) or {}).get("containers_list") or []


def _k8s_play_tasks() -> dict[str, dict]:
    play = next(
        p
        for p in yaml_fast.safe_load(DEPLOY.read_text())
        if p["name"] == "Deploy k8s workloads"
    )
    return {t["name"]: t for t in play["pre_tasks"] + play["tasks"]}


def _run_k8s_play(
    containers_list: list[dict], loop: str | None = None
) -> tuple[list[dict], list[str]]:
    """Evaluate the k8s play's containers_list facts and its batch loop over a host's list.

    Args:
        containers_list: The host's containers_list, before the play filters it.
        loop: A batch-loop expression to evaluate in place of deploy.yml's, for a red fixture.

    Returns:
        The containers_list fact the play's templates read, and the names the batch loop visits,
        flattened in batch order.
    """
    from ansible.parsing.dataloader import DataLoader
    from ansible.template import Templar, trust_as_template

    tasks = _k8s_play_tasks()
    variables: dict[str, Any] = {
        "containers_list_unfiltered": containers_list,
        "playbook_dir": str(ANSIBLE),
        "k8s_rollout_batch_width": 2,
    }
    for name in _FACT_TASKS:
        for fact, expr in tasks[name]["ansible.builtin.set_fact"].items():
            templar = Templar(loader=DataLoader(), variables=variables)
            templar.environment.filters.update(toposort.FilterModule().filters())
            variables[fact] = templar.template(trust_as_template(expr))
    templar = Templar(loader=DataLoader(), variables=variables)
    templar.environment.filters.update(toposort.FilterModule().filters())
    batches = templar.template(trust_as_template(loop or tasks[_BATCH_TASK]["loop"]))
    return variables["containers_list"], [c["name"] for batch in batches for c in batch]


# ── The filter ─────────────────────────────────────────────────────────────────────────────


def test_an_entry_without_owner_is_kept_and_one_with_owner_is_dropped():
    containers = [
        {"name": "a"},
        {"name": "f", "owner": "flux"},
        {"name": "b"},
        {"name": "r", "owner": "released"},
    ]
    assert _names(ansible_owned(containers)) == ["a", "b"]


@pytest.mark.parametrize("owner", ["Flux", "ansible", "", None, ["flux"]])
def test_an_owner_value_outside_the_two_raises(owner):
    with pytest.raises(AnsibleFilterError, match="has owner"):
        ansible_owned([{"name": "a"}, {"name": "x", "owner": owner}])


def test_the_input_list_is_not_mutated():
    containers = [{"name": "a"}, {"name": "f", "owner": "flux"}]
    ansible_owned(containers)
    assert _names(containers) == ["a", "f"]


def test_the_filter_accepts_the_same_owner_values_as_tag_owners():
    assert OWNER_VALUES == SCRIPT_OWNER_VALUES


# ── The play ───────────────────────────────────────────────────────────────────────────────


def test_the_batch_loop_skips_owned_entries_that_templates_still_see():
    live = [
        c
        for c in _containers(HOST_VARS / "daniel-box.yml")
        if c.get("platform") == "k8s"
    ]
    fixtures = [
        {"name": "fixture-flux", "platform": "k8s", "owner": "flux"},
        {"name": "fixture-released", "platform": "k8s", "owner": "released"},
    ]
    # Mid-list, so a filter that kept them would put them inside a batch, not after the last.
    middle = len(live) // 2
    fact, visited = _run_k8s_play(live[:middle] + fixtures + live[middle:])

    assert {"fixture-flux", "fixture-released"} <= set(_names(fact)), (
        "the containers_list fact lost an owned entry, so a template reading it would stop "
        "seeing a service Flux applies"
    )
    # bento-pdf has carried `owner: flux` since D2.7, peanut since D3.6, texbrain since D4.5,
    # the three Wave A exporters since D5.5, deploy-ui, littlelink and longhorn-ui since D5.6,
    # homepage and node-exporter since D5.7, loki-homelab, navidrome and scrutiny since D5.8,
    # homelab-mcp and ical-proxy since D5.14, bazarr, freshrss, livesync and speedtest since D5.9,
    # terraria and valheim since D5.15, and mosquitto, radarr and sonarr since D5.20, so the live
    # list holds owned entries too.
    owned = {"fixture-flux", "fixture-released"} | {
        c["name"] for c in live if "owner" in c
    }
    assert {
        "bazarr",
        "bento-pdf",
        "deploy-ui",
        "freshrss",
        "gpu-exporter",
        "homelab-mcp",
        "homepage",
        "ical-proxy",
        "littlelink",
        "livesync",
        "loki-homelab",
        "mosquitto",
        "longhorn-ui",
        "navidrome",
        "node-exporter",
        "nut-exporter",
        "peanut",
        "pihole-exporter",
        "radarr",
        "scrutiny",
        "sonarr",
        "speedtest",
        "terraria",
        "texbrain",
        "valheim",
    } <= owned
    assert visited == [n for n in _names(fact) if n not in owned]


# ── On every host's live containers_list ────────────────────────────────────────────────────


def _owner_drop_mismatch(containers: list[dict], select) -> str:
    """Why `select` does not drop exactly the entries carrying `owner`, in order; "" if it does."""
    kept = _names(select(containers))
    expected = [c["name"] for c in containers if "owner" not in c]
    if kept == expected:
        return ""
    dropped = [n for n in _names(containers) if n not in kept]
    carrying = [c["name"] for c in containers if "owner" in c]
    return (
        f"kept {kept} but expected {expected}; dropped {dropped}, owner on {carrying}"
    )


def _play_visit_mismatch(containers: list[dict], loop: str | None = None) -> str:
    """Why the batch loop does not visit the toposorted list less its owned entries; "" if so."""
    fact, visited = _run_k8s_play(containers, loop)
    expected = [c["name"] for c in fact if "owner" not in c]
    if visited == expected:
        return ""
    return f"the batch loop visits {visited}, but the toposorted Ansible-owned list is {expected}"


@pytest.mark.parametrize("path", _host_var_files(), ids=lambda p: p.stem)
def test_ansible_owned_drops_exactly_the_entries_carrying_owner_is_clean(path):
    containers = _containers(path)
    assert _owner_drop_mismatch(containers, ansible_owned) == ""

    if any(c.get("platform") == "k8s" for c in containers):
        assert _play_visit_mismatch(containers) == ""


def test_a_filter_dropping_an_entry_without_owner_is_flagged():
    containers = _containers(HOST_VARS / "daniel-box.yml")
    assert _owner_drop_mismatch(containers, lambda cs: ansible_owned(cs)[:-1]) != ""


def test_a_batch_loop_dropping_an_entry_without_owner_is_flagged():
    # The loop skips bento-pdf, the live list's one owned entry, as it must, but it also drops
    # the last Ansible-owned entry. That makes it the play half's red case: the visit check the
    # clean half runs on the live list must flag the missing entry.
    loop = "{{ (containers_list | ansible_owned)[:-1] | batch(2) | list }}"
    assert _play_visit_mismatch(_containers(HOST_VARS / "daniel-box.yml"), loop) != ""


def test_the_host_census_names_daniel_box_with_its_k8s_entries():
    assert "daniel-box" in {p.stem for p in _host_var_files()}
    k8s = [
        c
        for c in _containers(HOST_VARS / "daniel-box.yml")
        if c.get("platform") == "k8s"
    ]
    assert len(k8s) >= 30
