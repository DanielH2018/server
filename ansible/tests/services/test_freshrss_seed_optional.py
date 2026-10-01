"""`freshrss-config` must have exactly one creator on every host.

Two paths can create the claim, and which one runs is `freshrss_k8s_manage_claim`:

- true  — `k8s/volume-claim` creates it, from that role's own `pvc.yaml.j2`, and copies the
          seed tree into it.
- false — `k8s/manifests` applies `freshrss/templates/pvc.yaml.j2`, and the volume starts empty.

Both creating it is the failure this file exists for, and it is a quiet one: the two templates
render the same `kind`, `name` and `namespace`, so `kubectl apply` simply applies the second
over the first and reports success. Neither a schema check nor a render diff sees a problem.
Zero creators is the other half — the Deployment then references a claim nothing makes, and
nothing at admission verifies a referenced PVC exists, so that failure surfaces as a pod stuck
Pending long after the deploy reports green.

Why the flag exists at all: `freshrss_k8s_source_path` is a path on `seed_volume_source_host`
(daniel-server) that does not exist, because Docker is gone from that host. Prod never notices — its PV
already carries the seed label, so volume-claim short-circuits and never reads the source. A
cluster with a fresh PVC has no label, the copy decision resolves to true, and the deploy fails
trying to tar a directory that is gone.
"""

import ast

import pytest
from jinja2 import StrictUndefined


from _helpers import load_tasks
from _k8s_render import render_role_template
from lib.ansible_jinja_env import make_ansible_env
from lib import yaml_fast

from validate.k8s_manifests import (
    ALL_VARS,
    ANSIBLE,
    BASE_CONTEXT,
    K8S_ROLES,
    load_yaml,
    resolve_vars,
    role_defaults,
)

_ROLE = "freshrss"
_TASKS = K8S_ROLES / _ROLE / "tasks" / "main.yml"
_CLAIM = "freshrss-config"
_HOST = "daniel-box"
# The unseeded cluster, as an override rather than a host. Every host in the inventory
# seeds, so without this the guard below has no False to observe and a detector stuck on
# "seeded" would agree with every host.
_UNSEEDED = {"freshrss_k8s_manage_claim": False}


def _context(host: str, overrides: dict | None = None) -> dict:
    host_vars = ANSIBLE / "inventory" / "host_vars" / f"{host}.yml"
    base = {**BASE_CONTEXT, **load_yaml(ALL_VARS), **load_yaml(host_vars)}
    base["playbook_dir"] = str(ANSIBLE)
    base = resolve_vars(base, base)
    # Role defaults FIRST: Ansible ranks host_vars above them, and an override ranks above
    # both, the way `-e` does.
    return {**role_defaults(_ROLE, base), **base, **(overrides or {})}


def _include(task: dict) -> dict:
    return task.get("ansible.builtin.include_role") or task.get("include_role") or {}


def seed_runs(host: str, overrides: dict | None = None) -> bool:
    """Whether the k8s/volume-claim include's `when:` holds for this host."""
    ctx = _context(host, overrides)
    env = make_ansible_env(undefined_cls=StrictUndefined)
    for task in load_tasks(_TASKS):
        if _include(task).get("name") != "k8s/volume-claim":
            continue
        when = task.get("when")
        if when is None:
            return True
        return env.from_string("{{ " + when + " }}").render(ctx).strip() == "True"
    return False


def manifest_files(host: str, overrides: dict | None = None) -> list[str]:
    """The manifest list k8s/manifests is handed, with this host's variables applied."""
    ctx = _context(host, overrides)
    env = make_ansible_env(undefined_cls=StrictUndefined)
    for task in load_tasks(_TASKS):
        if _include(task).get("name") != "k8s/manifests":
            continue
        value = (task.get("vars") or {}).get("manifests_files")
        if isinstance(value, str):
            return ast.literal_eval(env.from_string(value).render(ctx))
        return value or []
    return []


def seed_claim_vars(host: str, overrides: dict | None = None) -> dict:
    """The variables k8s/volume-claim renders its PVC with, as the include hands them over.

    Read from the include task's own `vars:` and rendered against this host, laid over
    volume-claim's defaults the way Ansible ranks them. Hand-mapping freshrss's variables onto
    volume-claim's here would let a swapped mapping in `tasks/main.yml` pass unseen.
    """
    ctx = _context(host, overrides)
    env = make_ansible_env(undefined_cls=StrictUndefined)
    for task in load_tasks(_TASKS):
        if _include(task).get("name") != "k8s/volume-claim":
            continue
        handed = {
            key: env.from_string(value).render(ctx) if isinstance(value, str) else value
            for key, value in (task.get("vars") or {}).items()
        }
        return {**role_defaults("volume-claim", ctx), **ctx, **handed}
    raise AssertionError(f"{_TASKS} no longer includes k8s/volume-claim")


def creators(host: str, overrides: dict | None = None) -> list[str]:
    """Every path that would create the claim on this host."""
    out = []
    if seed_runs(host, overrides):
        out.append("k8s/volume-claim")
    if "pvc.yaml" in manifest_files(host, overrides):
        out.append("freshrss/templates/pvc.yaml.j2")
    return out


def creator_problem(found: list[str]) -> str:
    """The verdict, taking its list as an argument so the rejecting test drives the same code.

    Empty string means exactly one creator.
    """
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


@pytest.mark.parametrize("overrides", [None, _UNSEEDED], ids=["seeded", "unseeded"])
def test_the_claim_has_exactly_one_creator(overrides: dict | None) -> None:
    problem = creator_problem(creators(_HOST, overrides))
    assert not problem, f"{_HOST} with {overrides}: {problem}"


def test_a_seeded_cluster_and_an_unseeded_one_get_different_creators() -> None:
    """Pins which creator each side gets, so a flipped default is not silently absorbed by
    the count check above — one creator is one creator either way round."""
    assert creators(_HOST) == ["k8s/volume-claim"]
    assert creators(_HOST, _UNSEEDED) == ["freshrss/templates/pvc.yaml.j2"]


@pytest.mark.parametrize("overrides", [None, _UNSEEDED], ids=["seeded", "unseeded"])
def test_the_two_creators_agree_on_the_claim(overrides: dict | None) -> None:
    """The claim is one object under one name, so the seeded and unseeded clusters must not
    differ in storage class or size. Nothing else compares them — they live in different
    roles, and only one renders per host."""
    ctx = _context(_HOST, overrides)
    env = make_ansible_env(undefined_cls=StrictUndefined)
    seed_pvc = yaml_fast.safe_load(
        env.from_string(
            (K8S_ROLES / "volume-claim" / "templates" / "pvc.yaml.j2").read_text()
        ).render(seed_claim_vars(_HOST, overrides))
    )
    role_pvc = yaml_fast.safe_load(
        env.from_string(
            (K8S_ROLES / _ROLE / "templates" / "pvc.yaml.j2").read_text()
        ).render(ctx)
    )
    assert role_pvc == seed_pvc


def test_the_deployment_references_the_claim_the_flag_creates() -> None:
    """Ties the two halves together: the name the PVC renders is the name the pod mounts.

    A rename on one side alone passes every check above.
    """
    ctx = _context(_HOST, _UNSEEDED)
    env = make_ansible_env(undefined_cls=StrictUndefined)
    pvc = yaml_fast.safe_load(
        env.from_string(
            (K8S_ROLES / _ROLE / "templates" / "pvc.yaml.j2").read_text()
        ).render(ctx)
    )
    assert pvc["metadata"]["name"] == ctx["freshrss_k8s_claim"]
    deployment = yaml_fast.safe_load(
        render_role_template(_ROLE, "deployment.yaml.j2", _UNSEEDED)
    )
    mounted = {
        claim
        for volume in deployment["spec"]["template"]["spec"].get("volumes") or []
        if (claim := (volume.get("persistentVolumeClaim") or {}).get("claimName"))
    }
    assert mounted, (
        "the rendered Deployment mounts no claim, so the check below is vacuous"
    )
    assert pvc["metadata"]["name"] in mounted, (
        f"the Deployment mounts {sorted(mounted)}, not the {pvc['metadata']['name']!r} the "
        f"PVC renders — the pod sits Pending on a claim nothing creates"
    )


@pytest.mark.parametrize(
    ("found", "expect"),
    [
        (["k8s/volume-claim", "pvc.yaml"], "the second silently wins"),
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
    assert creator_problem(["k8s/volume-claim"]) == ""
