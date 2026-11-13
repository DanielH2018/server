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
    _rollout_gate_offender,
    _sets_empty_rollout,
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


def test_rollout_gate_flags_a_batch_role_that_does_not_gate_its_workload(
    widget_role,
) -> None:
    """A batch-only role rendering an ungated Job is still an offender.

    Rendering a batch workload is not itself proof of a gate — the gate must actually credit
    that workload's own name, or nothing here has proven anything.
    """
    role = widget_role(
        defaults="manifests_service: widget\nmanifests_rollout: ''\n",
        templates={},
    )
    (role / "templates" / "job.yaml.j2").write_text(
        "apiVersion: batch/v1\nkind: Job\nmetadata:\n  name: widget\n"
    )
    assert _rollout_gate_offender(role) is True


def test_rollout_gate_flags_a_role_rendering_no_workloads(widget_role) -> None:
    """A role setting `manifests_rollout: ''` and rendering nothing at all stays an offender.

    Rendering no workloads means the batch loop is vacuous, which could otherwise look
    identical to "everything it renders is gated." The three-way split is deliberate: a role
    with nothing to gate has also offered no evidence the deploy did anything, so it stays
    fail-closed rather than earning the same pass a fully-gated role gets.
    """
    role = widget_role(
        defaults="manifests_service: widget\nmanifests_rollout: ''\n",
        templates={},
    )
    assert _rollout_gate_offender(role) is True


def test_rollout_gate_tolerates_a_trailing_comment_on_the_empty_rollout(
    widget_role,
) -> None:
    """`manifests_rollout: ''  # nothing to roll` must still be seen as empty.

    This repo comments nearly every var, and that house style applied to `manifests_rollout`
    is the triggering edit. A role in this shape
    with no batch gate at all must still read as an offender — the comment must not make it
    invisible to the check.
    """
    role = widget_role(
        defaults="manifests_service: widget\n"
        "manifests_rollout: ''  # nothing to roll\n",
        templates={},
    )
    assert _sets_empty_rollout(role) is True
    assert _rollout_gate_offender(role) is True


def test_primary_rollout_name_agrees_with_sets_empty_rollout_on_a_comment(
    widget_role,
) -> None:
    """`_primary_rollout_name` must also read the trailing comment as empty, not the service.

    `_sets_empty_rollout` tolerates `manifests_rollout: ''  # nothing to roll`. A matcher
    anchored at end-of-line right after the closing quote would disagree about the same
    variable — one reads "empty", the other falls through to `manifests_service` and returns
    the real service name. That is a defect on its own, even while `_rollout_gate_offender`'s
    Deployment check happens to catch every affected role.
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


def test_rollout_gate_does_not_falsely_accuse_a_role_gated_via_extras(
    widget_role,
) -> None:
    """A role skipping the primary rollout but gating its Deployment via extras is not an offender.

    `manifests_extra_rollouts` rolls and soaks independently of the primary, so
    `manifests_rollout: ''` on the primary alone proves nothing here. An unconditional "renders
    a Deployment ⇒ offender" rule would flag this shape even though the Deployment IS gated.
    The rule checks `_ungated_deployments` (which already resolves gating by name,
    primary-or-extra) instead of "renders any Deployment at all".
    """
    role = widget_role(
        defaults="manifests_service: widget\n"
        "manifests_rollout: ''\n"
        "manifests_extra_rollouts:\n"
        "  - name: widget-extra\n",
        templates={},
    )
    (role / "templates" / "deployment-extra.yaml.j2").write_text(
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: widget-extra\n"
    )
    assert _ungated_deployments(role) == []
    assert _rollout_gate_offender(role) is False


def test_auto_deployable_roles_do_not_skip_the_rollout_gate() -> None:
    offenders = [
        f"{role.name}: passes manifests_rollout: '' with no gate proven — the rollout wait and "
        "stability soak are both skipped for the primary rollout (any manifests_extra_rollouts "
        "still roll and soak), and either it renders a Deployment/DaemonSet, or no batch "
        "workload, or a batch workload this role does not gate"
        for role in _roles()
        if _auto_deployable(role) and _rollout_gate_offender(role)
    ]
    assert not offenders, (
        "Auto-deployable role(s) with no rollout gate at all. For a role rendering a "
        "Deployment, that means restoring the rollout wait. For a batch-only role, gate every "
        "rendered Job with a role-local `wait --for=condition=complete job/<name>`. A rendered "
        "CronJob is gated only once the role is flux-owned and names it in "
        "`flux_cronjob_gates` (docs/flux-deploy-plane.md). Or set k8s_autodeploy: false with a "
        "k8s_autodeploy_reason in the role's own defaults/main.yml (the denylist is derived "
        "from that declaration):\n" + "\n".join(offenders)
    )
