"""Crediting a Deployment or DaemonSet as gated by roles/k8s/manifests.

`manifests` waits on the primary rollout plus every `manifests_extra_rollouts` entry, and
matching is by `metadata.name`. So a rendered Deployment is gated only if its name is in that
set — an undeclared one, a typo'd extra, and a name that is a Jinja expression all read as
ungated, which is the fail-closed direction.

A gate that returns before the pod is really up is the other half: a Deployment with no
readinessProbe passes `rollout status` the moment it reports Running, which proves the image
exists and nothing else.
"""

from __future__ import annotations

import re
import yaml
from pathlib import Path

from _autodeploy import (
    _K8S_ROLES,
    _auto_deployable,
    _declares_autodeploy,
    _deployment_templates,
    _roles,
)
from _autodeploy_rollout import (
    _MANIFEST_KIND_TO_ROLLOUT_KIND,
    _deployment_name,
    _deployments_missing_readiness_probe,
    _extra_rollouts,
    _gated_names,
    _primary_rollout_kind,
    _primary_rollout_name,
    _rollout_gate_offender,
    _sets_empty_rollout,
    _ungated_deployment_count,
    _ungated_deployments,
)


def test_auto_deployable_reads_the_declaration_not_the_denylist() -> None:
    """The guards' input is the role's own declaration.

    Asserted directly so that when slice 1b deletes the denylist, this file needs no edit —
    and so a future reader cannot mistake the denylist for the source of truth.
    """
    for role in _roles():
        if not _declares_autodeploy(role):
            continue
        data = yaml.safe_load((role / "defaults/main.yml").read_text()) or {}
        assert _auto_deployable(role) is bool(data.get("k8s_autodeploy"))


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


def test_auto_deployable_roles_gate_the_right_kind() -> None:
    """Name-only gating passes a role whose rollout kind is wrong.

    `_gated_names` compares names and nothing else, so a role setting
    `manifests_rollout: node-exporter` without `manifests_rollout_kind: daemonset` reports zero
    offenders while the shared role runs `rollout status deploy/node-exporter` against a
    Deployment that does not exist. kubectl fails loudly at deploy time; CI stays green, which
    is the failure this file exists to prevent.
    """
    offenders = []
    for role in _roles():
        if not _auto_deployable(role):
            continue
        primary = _primary_rollout_name(role)
        if not primary:
            continue
        declared = _primary_rollout_kind(role)
        for name in _deployment_templates(role):
            template = role / "templates" / name
            if _deployment_name(template) != primary:
                continue
            rendered = re.search(
                r"^kind:\s*(Deployment|DaemonSet)\s*$",
                template.read_text(),
                re.MULTILINE,
            )
            expected = _MANIFEST_KIND_TO_ROLLOUT_KIND[rendered.group(1)]
            if expected != declared:
                offenders.append(
                    f"{role.name}: {name} renders a {rendered.group(1)}, so the rollout gate "
                    f"needs manifests_rollout_kind: {expected}, but the role declares "
                    f"{declared!r}"
                )
    assert not offenders, (
        "Auto-deployable role(s) whose rollout gate names the wrong kind — `rollout status` "
        "would target a workload that does not exist:\n" + "\n".join(offenders)
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

    The old `any()` check would read this role as compliant — the primary has a probe, and
    `any()` stops looking once one template has one. Checking each template individually is
    what catches the extra.
    """
    role = widget_role(
        "- ansible.builtin.include_role:\n"
        "    name: k8s/manifests\n"
        "  vars:\n"
        "    manifests_service: widget\n"
        "    manifests_extra_rollouts:\n"
        "      - name: widget-cache\n",
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

    Before this, `_deployment_templates` matched only `kind: Deployment`, so a DaemonSet role
    rendered zero workloads and passed every shape guard while being ungated — the failure
    mode the guards exist to catch, hidden by the matcher rather than absent.
    """
    role = widget_role(
        templates={
            "daemonset.yaml.j2": "apiVersion: apps/v1\nkind: DaemonSet\nmetadata:\n  name: widget\n"
        },
    )
    assert _deployment_templates(role) == ["daemonset.yaml.j2"]
    assert _deployment_name(role / "templates/daemonset.yaml.j2") == "widget"


def test_extra_rollouts_are_counted_as_gated() -> None:
    """prowlarr renders two Deployments and gates both, by name — it is not an offender.

    This pins the guard's matching model (identity, not count) against a role that actually
    has an extra: a rendered Deployment is gated only if its own resolved name equals the
    primary rollout name or appears in `manifests_extra_rollouts`. prowlarr stays on the
    denylist regardless — the migrating-state PVC/Recreate shape covered in its
    `k8s_autodeploy_reason`, unrelated to whether it gates cleanly — so this test is about the
    guard's model, not about prowlarr's eligibility.
    """
    prowlarr = _K8S_ROLES / "prowlarr"
    assert len(_deployment_templates(prowlarr)) == 2
    assert _primary_rollout_name(prowlarr) == "prowlarr"
    assert _extra_rollouts(prowlarr) == {"flaresolverr"}
    assert (
        _deployment_name(prowlarr / "templates" / "deployment-flaresolverr.yaml.j2")
        == "flaresolverr"
    )
    assert _ungated_deployment_count(prowlarr) == 0


def test_extra_rollout_naming_the_wrong_deployment_reads_as_ungated(
    widget_role,
) -> None:
    """A typo'd or drifted `manifests_extra_rollouts` name doesn't gate anything real.

    Matching by count alone (rendered - 1 - len(extras) == 0) read this as fully gated even
    though the declared extra's name matches neither rendered Deployment. Matching by identity
    catches it: the second Deployment's real name isn't in {primary, declared extra}.
    """
    role = widget_role(
        "- ansible.builtin.include_role:\n"
        "    name: k8s/manifests\n"
        "  vars:\n"
        "    manifests_service: widget\n"
        "    manifests_extra_rollouts:\n"
        "      - name: widget-typo\n",
        templates={},
    )
    (role / "templates" / "deployment.yaml.j2").write_text(
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: widget\n"
    )
    (role / "templates" / "deployment-cache.yaml.j2").write_text(
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: widget-cache\n"
    )
    assert _extra_rollouts(role) == {"widget-typo"}
    assert _ungated_deployments(role) == ["deployment-cache.yaml.j2"]
    assert _ungated_deployment_count(role) == 1


def test_rollout_gate_credits_a_fully_gated_batch_role(widget_role) -> None:
    """A batch-only role that gates every rendered workload is not an offender.

    `manifests_rollout: ''` is correct and unavoidable here — there is no Deployment to roll —
    and the role-local `wait --for=condition=complete job/widget` is the alternative gate. This
    is the positive-proof case `_rollout_gate_offender` exists to recognise.
    """
    role = widget_role(
        "- ansible.builtin.include_role:\n"
        "    name: k8s/manifests\n"
        "  vars:\n"
        "    manifests_service: widget\n"
        "    manifests_rollout: ''\n"
        "- ansible.builtin.command:\n"
        "    cmd: kubectl wait --for=condition=complete job/widget --timeout=180s\n",
        templates={},
    )
    (role / "templates" / "job.yaml.j2").write_text(
        "apiVersion: batch/v1\nkind: Job\nmetadata:\n  name: widget\n"
    )
    assert _rollout_gate_offender(role) is False


def test_rollout_gate_flags_a_batch_role_that_does_not_gate_its_workload(
    widget_role,
) -> None:
    """A batch-only role rendering an ungated Job is still an offender.

    Rendering a batch workload is not itself proof of a gate — the gate must actually credit
    that workload's own name, or nothing here has proven anything.
    """
    role = widget_role(
        "- ansible.builtin.include_role:\n"
        "    name: k8s/manifests\n"
        "  vars:\n"
        "    manifests_service: widget\n"
        "    manifests_rollout: ''\n",
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
        "- ansible.builtin.include_role:\n"
        "    name: k8s/manifests\n"
        "  vars:\n"
        "    manifests_service: widget\n"
        "    manifests_rollout: ''\n",
        templates={},
    )
    assert _rollout_gate_offender(role) is True


def test_rollout_gate_tolerates_a_trailing_comment_on_the_empty_rollout(
    widget_role,
) -> None:
    """`manifests_rollout: ''  # nothing to roll` must still be seen as empty.

    task-3-rulings.md R3: this repo comments nearly every var, and the triggering edit for
    this gap is exactly that house style applied to `manifests_rollout`. A role in this shape
    with no batch gate at all must still read as an offender — the comment must not make it
    invisible to the check.
    """
    role = widget_role(
        "- ansible.builtin.include_role:\n"
        "    name: k8s/manifests\n"
        "  vars:\n"
        "    manifests_service: widget\n"
        "    manifests_rollout: ''  # nothing to roll\n",
        templates={},
    )
    assert _sets_empty_rollout(role) is True
    assert _rollout_gate_offender(role) is True


def test_primary_rollout_name_agrees_with_sets_empty_rollout_on_a_comment(
    widget_role,
) -> None:
    """`_primary_rollout_name` must also read the trailing comment as empty, not the service.

    task-3-rulings-2.md S4: R3 widened `_sets_empty_rollout` for
    `manifests_rollout: ''  # nothing to roll` and left this matcher anchored at end-of-line
    right after the closing quote, so the two disagreed about the same variable — one read
    "empty", the other fell through to `manifests_service` and returned the real service name.
    Latent while `_rollout_gate_offender`'s Deployment check happened to catch every affected
    role anyway, but two matchers disagreeing about one variable is a defect on its own.
    """
    role = widget_role(
        "- ansible.builtin.include_role:\n"
        "    name: k8s/manifests\n"
        "  vars:\n"
        "    manifests_service: widget\n"
        "    manifests_rollout: ''  # nothing to roll\n"
    )
    assert _primary_rollout_name(role) == ""


def _widget_with_a_deployment(
    widget_role, deployment_doc: str, deployment_filename: str = "deployment.yaml.j2"
) -> Path:
    """A role gating one batch workload (widget-job) while also rendering a Deployment.

    Shared by the three task-3-rulings.md R2 control cases below — they differ only in how
    the Deployment's `kind:` line is spelled, or whether it shares a file with another
    document.
    """
    role = widget_role(
        "- ansible.builtin.include_role:\n"
        "    name: k8s/manifests\n"
        "  vars:\n"
        "    manifests_service: widget\n"
        "    manifests_rollout: ''\n"
        "- name: Wait for widget-job\n"
        "  tags: [deploy]\n"
        "  ansible.builtin.command:\n"
        "    cmd: k3s kubectl wait --for=condition=complete job/widget-job --timeout=120s\n",
        templates={},
    )
    (role / "templates" / "job.yaml.j2").write_text(
        "apiVersion: batch/v1\nkind: Job\nmetadata:\n  name: widget-job\n"
    )
    (role / "templates" / deployment_filename).write_text(deployment_doc)
    return role


def test_rollout_gate_still_flags_a_role_with_a_quoted_kind_deployment(
    widget_role,
) -> None:
    """A fully-gated batch workload does not exempt a role that also renders a Deployment.

    task-3-rulings.md R2: `_rollout_gate_offender` used to check only the batch workloads it
    could see, never whether the role also rendered a Deployment or DaemonSet. Paired with
    `_deployment_templates` being blind to `kind: "Deployment"` — valid YAML, applied by
    kubectl exactly like the bare form — a role in this shape read as a fully-gated batch-only
    role while its Deployment had no rollout wait at all.
    """
    role = _widget_with_a_deployment(
        widget_role,
        'apiVersion: apps/v1\nkind: "Deployment"\nmetadata:\n  name: widget\n',
    )
    assert _rollout_gate_offender(role) is True


def test_rollout_gate_still_flags_a_role_with_a_commented_kind_deployment(
    widget_role,
) -> None:
    """Same control as the quoted-kind case, for `kind: Deployment  # web`."""
    role = _widget_with_a_deployment(
        widget_role,
        "apiVersion: apps/v1\nkind: Deployment  # web\nmetadata:\n  name: widget\n",
    )
    assert _rollout_gate_offender(role) is True


def test_rollout_gate_still_flags_a_role_with_a_split_document_deployment(
    widget_role,
) -> None:
    """Same control, for a Deployment sharing a `---`-split template with another document."""
    role = _widget_with_a_deployment(
        widget_role,
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: widget-cfg\n"
        "---\n"
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: widget\n",
        deployment_filename="deployment-pair.yaml.j2",
    )
    assert _rollout_gate_offender(role) is True


def test_rollout_gate_flags_one_gated_and_one_ungated_batch_workload(
    widget_role,
) -> None:
    """A role gating one of two rendered Jobs is still an offender, not exempt.

    task-3-rulings.md R4: `_rollout_gate_offender`'s final line uses `any(...)`, which is
    correct — a role must gate EVERY batch workload it renders, not just one. Every test in
    this file up to this one renders at most one batch workload, so a bug that quietly swapped
    `any` for `all` would leave all of them green. This fixture is the one that would go red
    under that substitution.
    """
    role = widget_role(
        "- ansible.builtin.include_role:\n"
        "    name: k8s/manifests\n"
        "  vars:\n"
        "    manifests_service: widget\n"
        "    manifests_rollout: ''\n"
        "- name: Wait for widget-a\n"
        "  tags: [deploy]\n"
        "  ansible.builtin.command:\n"
        "    cmd: k3s kubectl wait --for=condition=complete job/widget-a --timeout=120s\n",
        templates={},
    )
    (role / "templates" / "job-a.yaml.j2").write_text(
        "apiVersion: batch/v1\nkind: Job\nmetadata:\n  name: widget-a\n"
    )
    (role / "templates" / "job-b.yaml.j2").write_text(
        "apiVersion: batch/v1\nkind: Job\nmetadata:\n  name: widget-b\n"
    )
    assert _rollout_gate_offender(role) is True


def test_rollout_gate_does_not_falsely_accuse_a_role_gated_via_extras(
    widget_role,
) -> None:
    """A role skipping the primary rollout but gating its Deployment via extras is not an offender.

    task-3-rulings-2.md S5: R2's unconditional "renders a Deployment ⇒ offender" flagged this shape
    even though the Deployment IS gated — `manifests_extra_rollouts` rolls and soaks independently
    of the primary, so `manifests_rollout: ''` on the primary alone proves nothing here. The
    reviewer measured `_rollout_gate_offender: True` while `_ungated_deployments: []` for exactly
    this construction. The generalised rule checks `_ungated_deployments` (which already resolves
    gating by name, primary-or-extra) instead of "renders any Deployment at all", so this is no
    longer a false offender.
    """
    role = widget_role(
        "- ansible.builtin.include_role:\n"
        "    name: k8s/manifests\n"
        "  vars:\n"
        "    manifests_service: widget\n"
        "    manifests_rollout: ''\n"
        "    manifests_extra_rollouts:\n"
        "      - name: widget-extra\n",
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
        "rendered Job with a role-local `wait --for=condition=complete job/<name>`, or every "
        "rendered CronJob with `include_role: k8s/cronjob-gate` "
        "(`cronjob_gate_name: <the CronJob's metadata.name>`) — see "
        "roles/k8s/cronjob-gate/CLAUDE.md. Or set k8s_autodeploy: false with a "
        "k8s_autodeploy_reason in the role's own defaults/main.yml (the denylist is derived "
        "from that declaration):\n" + "\n".join(offenders)
    )
