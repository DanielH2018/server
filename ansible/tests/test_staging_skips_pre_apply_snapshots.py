"""The staging cluster must not take pre-apply Longhorn snapshots, and prod must.

`k8s/manifests` includes `k8s/volume-snapshot` before applying a role's manifests, gated on
`k8s_autodeploy_snapshot_pvcs` being non-empty. `daniel-stage.yml` sets that to `[]`, which
outranks every role's own declaration and so turns the include off host-wide.

That is intended, and it is load-bearing rather than tidy-up: `k8s/volume-snapshot` resolves
its deploy tag with `git rev-parse` and `chdir` into `{{ playbook_dir }}/..` **on the target**.
daniel-box is `connection=local` in `hosts.ini`, so the checkout is there; daniel-stage is a
genuinely remote host with no checkout, and the task fails with `Unable to change directory
before execution`. Staging's first freshrss deploy failed exactly there.

The gate is read out of `k8s/manifests/tasks/main.yml` and evaluated under each host's real
variables, rather than restated here. A test that asserted `k8s_autodeploy_snapshot_pvcs == []`
directly would keep passing if the include's `when:` stopped consulting the list.
"""

from __future__ import annotations

import sys

import pytest
import yaml
from jinja2 import Environment, StrictUndefined
from _helpers import REPO

_REPO = REPO
sys.path.insert(0, str(_REPO / "scripts"))

from validate_k8s_manifests import (  # noqa: E402 — needs the path insert above
    ALL_VARS,
    ANSIBLE,
    BASE_CONTEXT,
    K8S_ROLES,
    load_yaml,
    register_ansible_filters,
    resolve_vars,
    role_defaults,
)

_INCLUDED_ROLE = "k8s/volume-snapshot"


def _context(host: str, role: str) -> dict:
    host_vars = ANSIBLE / "inventory" / "host_vars" / f"{host}.yml"
    base = {**BASE_CONTEXT, **load_yaml(ALL_VARS), **load_yaml(host_vars)}
    base["playbook_dir"] = str(ANSIBLE)
    base = resolve_vars(base, base)
    # Role defaults FIRST: Ansible ranks host_vars above them, which is the whole mechanism
    # under test — the host's empty list must beat the role's own declaration.
    return {**role_defaults(role, base), **base}


def _snapshot_gate() -> list[str]:
    """The `when:` conditions on the volume-snapshot include, read from the role itself."""
    tasks = yaml.safe_load((K8S_ROLES / "manifests" / "tasks" / "main.yml").read_text())
    for task in tasks:
        include = (
            task.get("ansible.builtin.include_role") or task.get("include_role") or {}
        )
        if include.get("name") != _INCLUDED_ROLE:
            continue
        when = task.get("when")
        if isinstance(when, str):
            return [when]
        return list(when or [])
    raise AssertionError(
        f"no include of {_INCLUDED_ROLE} found in k8s/manifests — this test is looking at "
        f"the wrong task, and its green says nothing"
    )


def snapshot_runs(host: str, role: str) -> bool:
    """Whether the pre-apply snapshot include fires for this host and role."""
    ctx = _context(host, role)
    env = Environment(undefined=StrictUndefined)
    register_ansible_filters(env)
    return all(
        env.from_string("{{ " + cond + " }}").render(ctx).strip() == "True"
        for cond in _snapshot_gate()
    )


def _roles_of(host: str) -> list[str]:
    host_vars = load_yaml(ANSIBLE / "inventory" / "host_vars" / f"{host}.yml")
    return [
        c["name"]
        for c in host_vars.get("containers_list") or []
        if c.get("platform") == "k8s" and (K8S_ROLES / c["name"]).is_dir()
    ]


def test_the_gate_is_the_one_the_role_actually_carries() -> None:
    """Pins that the gate still consults the claim list. If someone rewrites the `when:` to
    read something else, every assertion below would keep passing against a stale idea of
    what turns the snapshot on."""
    assert any("k8s_autodeploy_snapshot_pvcs" in cond for cond in _snapshot_gate())


@pytest.mark.parametrize("role", _roles_of("daniel-stage"))
def test_staging_takes_no_pre_apply_snapshot(role: str) -> None:
    assert not snapshot_runs("daniel-stage", role), (
        f"daniel-stage would run {_INCLUDED_ROLE} for {role}. That role resolves its deploy "
        f"tag by chdir-ing into the checkout ON THE TARGET, and the staging guest has none — "
        f"the deploy fails with 'Unable to change directory before execution'."
    )


def test_prod_still_snapshots_freshrss() -> None:
    """The accepting half. Every staging assertion above is a negative, so without this a
    gate that had stopped firing anywhere would read entirely green."""
    assert snapshot_runs("daniel-box", "freshrss")


def test_the_evaluator_reports_true_when_the_gate_holds() -> None:
    """The rejecting half for the evaluator itself. `snapshot_runs` returns False for any
    unrenderable or absent condition, so a broken evaluator fails open into exactly the
    all-green shape the staging tests want. Prod's freshrss above is one real True; this
    pins that the mechanism, not the host, is what produced it."""
    assert snapshot_runs("daniel-box", "freshrss") is True
    assert snapshot_runs("daniel-stage", "freshrss") is False
