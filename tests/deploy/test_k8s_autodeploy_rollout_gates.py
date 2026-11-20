"""Crediting a Deployment or DaemonSet as gated by a role's rollout entries.

`manifests` waits on the primary rollout plus every `manifests_extra_rollouts` entry, and
matching is by `metadata.name`. So a rendered Deployment is gated only if its name is in that
set — an undeclared one, a typo'd extra, and a name that is a Jinja expression all read as
ungated, which is the fail-closed direction.

A gate that returns before the pod is really up is the other half: a Deployment with no
readinessProbe passes `rollout status` the moment it reports Running, which proves the image
exists and nothing else.
"""

from pathlib import Path

from _autodeploy import (
    _auto_deployable,
    _deployment_templates,
    _rendered_roles,
    _roles,
    _template_texts,
)
from _autodeploy_rollout import (
    _deployment_docs,
    _deployments_missing_readiness_probe,
    _gated_names,
    _primary_rollout_name,
    _ungated_deployments,
)


def test_the_render_reaches_every_auto_deployable_role() -> None:
    """The shape guards read renders, and `_k8s_render` skips caller-rendered roles.

    `_template_texts` falls back to template source for a role the render does not reach, so
    a guard over an auto-deployable role outside the render would judge source text again.
    pihole is the member that must be reached: it names both Deployments through a macro
    argument, which only a render resolves.
    """
    rendered = _rendered_roles()
    assert "pihole" in rendered
    auto = {role.name for role in _roles() if _auto_deployable(role)}
    assert sorted(auto - set(rendered)) == [], "auto-deployable roles the render skips"


def test_auto_deployable_roles_gate_every_deployment_they_render() -> None:
    offenders = []
    for role in _roles():
        if not _auto_deployable(role):
            continue
        ungated = _ungated_deployments(role)
        if ungated:
            offenders.append(
                f"{role.name}: {', '.join(ungated)} not in the gated set "
                f"{sorted(_gated_names(role))}"
            )
    assert not offenders, (
        "Auto-deployable role(s) with an ungated workload — declare the extras in "
        "manifests_extra_rollouts, or set k8s_autodeploy: false with a k8s_autodeploy_reason "
        "in the role's own defaults/main.yml (the denylist is derived from that "
        "declaration):\n" + "\n".join(offenders)
    )


def test_auto_deployable_roles_declare_a_readiness_probe() -> None:
    offenders = []
    for role in _roles():
        if not _auto_deployable(role):
            continue
        missing = _deployments_missing_readiness_probe(role)
        if missing:
            offenders.append(
                f"{role.name}: {', '.join(missing)} has no readinessProbe — "
                f"`rollout status` returns on Running"
            )
    assert not offenders, (
        "Auto-deployable role(s) whose rollout gate proves nothing — give the workload(s) a "
        "readinessProbe, or set k8s_autodeploy: false with a k8s_autodeploy_reason in the "
        "role's own defaults/main.yml (the denylist is derived from that declaration):\n"
        + "\n".join(offenders)
    )


def test_readiness_probe_check_covers_every_gated_deployment(widget_role) -> None:
    """A probe on the primary Deployment doesn't excuse a probe-less gated extra.

    An `any()` check would read this role as compliant — the primary has a probe, and
    `any()` stops looking once one template has one. Checking each template individually is
    what catches the extra.
    """
    role = widget_role(
        defaults="manifests_service: widget\n"
        "manifests_extra_rollouts:\n"
        "  - name: widget-cache\n",
        templates={},
    )
    (role / "templates" / "deployment.yaml.j2").write_text(
        "apiVersion: apps/v1\n"
        "kind: Deployment\n"
        "metadata:\n"
        "  name: widget\n"
        "spec:\n"
        "  template:\n"
        "    spec:\n"
        "      containers:\n"
        "        - readinessProbe:\n"
        "            httpGet:\n"
        "              path: /\n"
    )
    (role / "templates" / "deployment-cache.yaml.j2").write_text(
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: widget-cache\n"
    )
    assert _deployments_missing_readiness_probe(role) == ["deployment-cache.yaml.j2"]


def test_the_workload_matcher_sees_daemonsets(widget_role) -> None:
    """A DaemonSet-rendering role must be visible to the shape guards.

    Matching only `kind: Deployment` would make a DaemonSet role render zero workloads and pass
    every shape guard while being ungated — the failure mode the guards exist to catch, hidden
    by the matcher rather than absent.
    """
    role = widget_role(
        templates={
            "daemonset.yaml.j2": "apiVersion: apps/v1\nkind: DaemonSet\nmetadata:\n  name: widget\n"
        },
    )
    assert _deployment_templates(role) == ["daemonset.yaml.j2"]
    assert _deployment_docs(_template_texts(role)["daemonset.yaml.j2"]) == [
        ("DaemonSet", "widget")
    ]


def test_primary_rollout_name_reads_a_commented_empty_rollout_as_empty(
    widget_role,
) -> None:
    """`manifests_rollout: ''  # nothing to roll` names no primary rollout, not the service.

    A match anchored at end-of-line right after the closing quote would fall through to
    `manifests_service` and return the real service name.
    """
    role = widget_role(
        defaults="manifests_service: widget\nmanifests_rollout: ''  # nothing to roll\n"
    )
    assert _primary_rollout_name(role) == ""


def _widget_with_a_deployment(
    widget_role, deployment_doc: str, deployment_filename: str = "deployment.yaml.j2"
) -> Path:
    """A role that skips its primary rollout and renders one Deployment, spelled as given.

    Shared by the three control cases below. They differ only in how the Deployment's `kind:`
    line is spelled, or whether it shares a file with another document.
    """
    role = widget_role(
        defaults="manifests_service: widget\nmanifests_rollout: ''\n",
        templates={deployment_filename: deployment_doc},
    )
    return role


def test_a_quoted_kind_deployment_reads_as_ungated(widget_role) -> None:
    """`kind: "Deployment"` is valid YAML that kubectl applies exactly like the bare form.

    A matcher blind to it would let a role skip its rollout wait while rendering one.
    """
    role = _widget_with_a_deployment(
        widget_role,
        'apiVersion: apps/v1\nkind: "Deployment"\nmetadata:\n  name: widget-web\n',
    )
    assert _ungated_deployments(role) == ["deployment.yaml.j2"]


def test_a_commented_kind_deployment_reads_as_ungated(widget_role) -> None:
    """Same control as the quoted-kind case, for `kind: Deployment  # web`."""
    role = _widget_with_a_deployment(
        widget_role,
        "apiVersion: apps/v1\nkind: Deployment  # web\nmetadata:\n  name: widget-web\n",
    )
    assert _ungated_deployments(role) == ["deployment.yaml.j2"]


def test_a_split_document_deployment_reads_as_ungated(widget_role) -> None:
    """Same control, for a Deployment sharing a `---`-split template with another document."""
    role = _widget_with_a_deployment(
        widget_role,
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: widget-cfg\n"
        "---\n"
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: widget-web\n",
        deployment_filename="deployment-pair.yaml.j2",
    )
    assert _ungated_deployments(role) == ["deployment-pair.yaml.j2"]
