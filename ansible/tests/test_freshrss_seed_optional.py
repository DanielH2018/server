"""`freshrss-config` must have exactly one creator on every host.

Two paths can create the claim, and which one runs is `freshrss_k8s_manage_seed`:

- true  — `k8s/seed-volume` creates it, from that role's own `pvc.yaml.j2`, and copies the
          pre-migration Docker tree into it.
- false — `k8s/manifests` applies `freshrss/templates/pvc.yaml.j2`, and the volume starts empty.

Both creating it is the failure this file exists for, and it is a quiet one: the two templates
render the same `kind`, `name` and `namespace`, so `kubectl apply` simply applies the second
over the first and reports success. Neither a schema check nor a render diff sees a problem.
Zero creators is the other half — the Deployment then references a claim nothing makes, and
nothing at admission verifies a referenced PVC exists, so that failure surfaces as a pod stuck
Pending long after the deploy reports green.

Why the flag exists at all: `freshrss_k8s_source_path` is a path on `seed_volume_source_host`
(daniel-server) that Docker's removal on 2026-08-14 took with it. Prod never notices — its PV
already carries the seed label, so seed-volume short-circuits and never reads the source. A
cluster with a fresh PVC has no label, the copy decision resolves to true, and the deploy fails
trying to tar a directory that is gone.
"""

from __future__ import annotations

import ast
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

_ROLE = "freshrss"
_CLAIM = "freshrss-config"
_HOSTS = ("daniel-box", "daniel-stage")


def _context(host: str) -> dict:
    host_vars = ANSIBLE / "inventory" / "host_vars" / f"{host}.yml"
    base = {**BASE_CONTEXT, **load_yaml(ALL_VARS), **load_yaml(host_vars)}
    base["playbook_dir"] = str(ANSIBLE)
    base = resolve_vars(base, base)
    # Role defaults FIRST: Ansible ranks host_vars above them, and a staging host exists to
    # override them.
    return {**role_defaults(_ROLE, base), **base}


def _tasks() -> list:
    return yaml.safe_load((K8S_ROLES / _ROLE / "tasks" / "main.yml").read_text())


def _include(task: dict) -> dict:
    return task.get("ansible.builtin.include_role") or task.get("include_role") or {}


def seed_runs(host: str) -> bool:
    """Whether the k8s/seed-volume include's `when:` holds for this host."""
    ctx = _context(host)
    env = Environment(undefined=StrictUndefined)
    register_ansible_filters(env)
    for task in _tasks():
        if _include(task).get("name") != "k8s/seed-volume":
            continue
        when = task.get("when")
        if when is None:
            return True
        return env.from_string("{{ " + when + " }}").render(ctx).strip() == "True"
    return False


def manifest_files(host: str) -> list[str]:
    """The manifest list k8s/manifests is handed, with this host's variables applied."""
    ctx = _context(host)
    env = Environment(undefined=StrictUndefined)
    register_ansible_filters(env)
    for task in _tasks():
        if _include(task).get("name") != "k8s/manifests":
            continue
        value = (task.get("vars") or {}).get("manifests_files")
        if isinstance(value, str):
            return ast.literal_eval(env.from_string(value).render(ctx))
        return value or []
    return []


def creators(host: str) -> list[str]:
    """Every path that would create the claim on this host."""
    out = []
    if seed_runs(host):
        out.append("k8s/seed-volume")
    if "pvc.yaml" in manifest_files(host):
        out.append("freshrss/templates/pvc.yaml.j2")
    return out


def creator_problem(found: list[str]) -> str:
    """The verdict, taking its list as an argument so the rejecting test drives the same
    code. Empty string means exactly one creator."""
    if len(found) > 1:
        return (
            f"{_CLAIM} has {len(found)} creators ({found}) — both apply the same object "
            f"under the same name, and the second silently wins."
        )
    if not found:
        return (
            f"{_CLAIM} has no creator — the Deployment references a claim nothing makes, "
            f"and nothing at admission catches it. The pod sits Pending."
        )
    return ""


@pytest.mark.parametrize("host", _HOSTS)
def test_the_claim_has_exactly_one_creator(host: str) -> None:
    problem = creator_problem(creators(host))
    assert not problem, f"{host}: {problem}"


def test_prod_seeds_and_staging_does_not() -> None:
    """Pins which creator each host gets, so a flipped default is not silently absorbed by
    the count check above — one creator is one creator either way round."""
    assert creators("daniel-box") == ["k8s/seed-volume"]
    assert creators("daniel-stage") == ["freshrss/templates/pvc.yaml.j2"]


@pytest.mark.parametrize("host", _HOSTS)
def test_the_two_creators_agree_on_the_claim(host: str) -> None:
    """The claim is one object under one name, so the seeded and unseeded clusters must not
    differ in storage class or size. Nothing else compares them — they live in different
    roles, and only one renders per host."""
    ctx = _context(host)
    seed_pvc = yaml.safe_load(
        (K8S_ROLES / "seed-volume" / "templates" / "pvc.yaml.j2")
        .read_text()
        .replace("{{ seed_volume_claim }}", ctx["freshrss_k8s_claim"])
        .replace("{{ seed_volume_storage_class }}", ctx["freshrss_k8s_storage_class"])
        .replace("{{ seed_volume_size }}", ctx["freshrss_k8s_size"])
        .replace("{{ k8s_namespace }}", ctx["k8s_namespace"])
    )
    env = Environment(undefined=StrictUndefined)
    register_ansible_filters(env)
    role_pvc = yaml.safe_load(
        env.from_string(
            (K8S_ROLES / _ROLE / "templates" / "pvc.yaml.j2").read_text()
        ).render(ctx)
    )
    assert role_pvc == seed_pvc


def test_the_deployment_references_the_claim_the_flag_creates() -> None:
    """Ties the two halves together: the name the PVC template renders is the name the pod
    mounts. A rename on one side alone passes every check above."""
    ctx = _context("daniel-stage")
    env = Environment(undefined=StrictUndefined)
    register_ansible_filters(env)
    pvc = yaml.safe_load(
        env.from_string(
            (K8S_ROLES / _ROLE / "templates" / "pvc.yaml.j2").read_text()
        ).render(ctx)
    )
    assert pvc["metadata"]["name"] == ctx["freshrss_k8s_claim"]
    deployment = (K8S_ROLES / _ROLE / "templates" / "deployment.yaml.j2").read_text()
    assert "freshrss_k8s_claim" in deployment, (
        "the Deployment no longer names the claim through freshrss_k8s_claim, so the PVC "
        "template and the mount can drift apart"
    )


@pytest.mark.parametrize(
    ("found", "expect"),
    [
        (["k8s/seed-volume", "pvc.yaml"], "the second silently wins"),
        ([], "The pod sits Pending"),
    ],
)
def test_the_verdict_rejects_both_broken_combinations(
    found: list[str], expect: str
) -> None:
    """The rejecting half, driving the SAME function the real test drives.

    Both hosts land on exactly one creator today, so the assertion above is only ever observed
    passing and cannot show it would notice two or zero. Each direction is asserted separately
    because they fail differently: two creators is silent, zero is a pod that never starts.
    """
    assert expect in creator_problem(found)


def test_the_verdict_accepts_one_creator() -> None:
    """The accepting half, so a verdict that flagged everything would fail here."""
    assert creator_problem(["k8s/seed-volume"]) == ""
