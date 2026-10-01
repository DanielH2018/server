"""Every staging opt-out flag must render valid manifests on both branches and default on.

A staging cluster runs a subset of prod, so four `*_k8s_manage_*` flags switch a feature off
where its backend or credential does not exist (docs/archive/staging-cluster.md, Decisions 4
and 5). Each flag gates several blocks at once, and each has the same three failure shapes,
which this module checks once over the whole table:

- A stray conditional shifts indentation. That shows up as a parse error here and nowhere else.
- A half-gated template leaves a `volumeMounts` entry naming a volume that dropped out. The
  manifest is valid YAML and applies cleanly; the pod simply never starts.
- A flag that defaults off loses the feature on every cluster by omission, prod included.

What each flag gates, and the asymmetries between them, stay in the flag's own module:
`ansible/tests/services/test_traefik_acme_optional.py`, `test_livesync_gate_optional.py` and
`test_crowdsec_optional.py`.
"""

import pytest

from lib import yaml_fast

from validate.k8s_manifests import K8S_ROLES, load_yaml

from _k8s_render import render_role_template

# (role, flag, templates the flag edits). A template not listed here renders the same either way.
FLAGS = [
    (
        "traefik",
        "traefik_k8s_manage_acme",
        ("deployment.yaml.j2", "static-config.yaml.j2"),
    ),
    (
        "traefik",
        "traefik_k8s_manage_livesync_gate",
        ("deployment.yaml.j2", "static-config.yaml.j2"),
    ),
    (
        "traefik",
        "traefik_k8s_manage_crowdsec",
        ("deployment.yaml.j2", "static-config.yaml.j2", "dynamic.yaml.j2"),
    ),
    ("authelia", "authelia_k8s_manage_crowdsec", ("deployment.yaml.j2",)),
]

_RENDERS = [
    pytest.param(role, flag, template, id=f"{flag}-{template.removesuffix('.yaml.j2')}")
    for role, flag, templates in FLAGS
    for template in templates
]
_ROLE_FLAGS = [pytest.param(role, flag, id=flag) for role, flag, _ in FLAGS]


def _docs(role: str, flag: str, template: str, manage: bool) -> list[dict]:
    text = render_role_template(role, template, {flag: manage})
    return [d for d in yaml_fast.safe_load_all(text) if d is not None]


def dangling_mounts(spec: dict) -> list[str]:
    """`container/mount` for every volumeMount that names no volume in the pod spec."""
    declared = {v["name"] for v in spec.get("volumes", [])}
    return [
        f"{container['name']}/{mount['name']}"
        for container in spec.get("initContainers", []) + spec["containers"]
        for mount in container.get("volumeMounts", [])
        if mount["name"] not in declared
    ]


@pytest.mark.parametrize(("role", "flag", "template"), _RENDERS)
@pytest.mark.parametrize("manage", [True, False])
def test_both_branches_parse_as_yaml(
    role: str, flag: str, template: str, manage: bool
) -> None:
    assert _docs(role, flag, template, manage), (
        f"{role}/{template} rendered no YAML document with {flag}={manage}"
    )


@pytest.mark.parametrize(("role", "flag"), _ROLE_FLAGS)
@pytest.mark.parametrize("manage", [True, False])
def test_every_mount_resolves_to_a_declared_volume(
    role: str, flag: str, manage: bool
) -> None:
    # `safe_load`, not `_docs`: a stray second document in the Deployment must fail here.
    text = render_role_template(role, "deployment.yaml.j2", {flag: manage})
    spec = yaml_fast.safe_load(text)["spec"]["template"]["spec"]
    dangling = dangling_mounts(spec)
    assert not dangling, (
        f"{role} mounts {dangling}, which no volume declares ({flag}={manage})"
    )


def test_a_mount_whose_volume_dropped_out_is_flagged() -> None:
    spec = {
        "volumes": [{"name": "config"}],
        "initContainers": [{"name": "init", "volumeMounts": [{"name": "acme"}]}],
        "containers": [{"name": "app", "volumeMounts": [{"name": "config"}]}],
    }
    assert dangling_mounts(spec) == ["init/acme"]


def test_mounts_that_all_resolve_are_clean() -> None:
    spec = {
        "volumes": [{"name": "config"}],
        "containers": [{"name": "app", "volumeMounts": [{"name": "config"}]}],
    }
    assert dangling_mounts(spec) == []


@pytest.mark.parametrize(("role", "flag"), _ROLE_FLAGS)
def test_prod_manages_the_feature(role: str, flag: str) -> None:
    """Every flag defaults on, so no cluster loses the feature by omission."""
    assert load_yaml(K8S_ROLES / role / "defaults" / "main.yml")[flag] is True
