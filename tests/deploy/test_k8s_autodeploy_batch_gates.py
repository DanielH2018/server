"""Crediting a batch workload as gated: which `flux_cronjob_gates` entry counts.

A Job or CronJob is never gated through `manifests_rollout`, so a role that renders one must
carry its own completion gate. These are the shapes that must and must not be credited as that
gate — every case is a mutation somebody could plausibly write, and crediting a gate that does
not run is the fail-open direction.

Synthetic roles rather than live ones on purpose: a case pinned to a real role stops being a
regression test the day that role is retired. The `widget_role` fixture builds them.

This file holds how batch templates are read, the `flux_cronjob_gates` credit and the
tree-wide assertion.
"""

from _autodeploy import (
    _K8S_ROLES,
    _auto_deployable,
    _batch_templates,
    _roles,
)
from _autodeploy_batch import flux_gated_names


def test_batch_templates_sees_every_document_in_a_multi_job_template(
    widget_role,
) -> None:
    """A template with two `---`-separated Jobs must yield both, not just the first.

    registry's selftest-pull-job.yaml.j2 was the live instance until its flip deleted it: it
    rendered two Jobs in one file. A `_batch_templates` that stops at the first `name:` in the
    file would see only the former — an ungated second Job that no offender list would ever
    name, the same fail-open shape.
    """
    jobs = "".join(
        f"---\napiVersion: batch/v1\nkind: Job\nmetadata:\n  name: {name}\n"
        for name in ("widget-pull", "widget-pull-agent")
    )
    role = widget_role(templates={"pull-job.yaml.j2": jobs})
    found = {
        name
        for filename, name in _batch_templates(role)
        if filename == "pull-job.yaml.j2"
    }
    assert found == {"widget-pull", "widget-pull-agent"}


def test_batch_templates_sees_quoted_and_commented_kind(widget_role) -> None:
    """`kind: "Job"` and `kind: Job  # comment` must both be seen as batch templates.

    Both are valid YAML that kubectl applies identically to the bare `kind: Job` form. No
    live template uses either spelling, so this is prophylactic.
    """
    role = widget_role(
        templates={
            "quoted-job.yaml.j2": 'apiVersion: batch/v1\nkind: "Job"\nmetadata:\n  name: widget-quoted\n',
            "commented-job.yaml.j2": "apiVersion: batch/v1\nkind: Job  # one-shot\nmetadata:\n  name: widget-commented\n",
        },
    )
    found = dict(_batch_templates(role))
    assert found == {
        "quoted-job.yaml.j2": "widget-quoted",
        "commented-job.yaml.j2": "widget-commented",
    }


def test_auto_deployable_roles_gate_every_batch_workload_they_render() -> None:
    """An auto-deployable role must wait on every Job/CronJob it renders.

    Without this, a batch-only role auto-deploys with no gate at all: `rollout status` has
    no Deployment to watch, `manifests_rollout: ''` skips the stability soak too, and a bad
    image is reported as a successful deploy. A role delegating its batch workload to a
    shared role (image-builder) renders no Job/CronJob template of its own, so the inner
    loop below never runs for it and the role passes vacuously — a known limit, not a hole
    an exemption branch was ever needed to close (see the module docstring).
    """
    offenders = []
    for role in _roles():
        if not _auto_deployable(role):
            continue
        gated = flux_gated_names(role)
        for template, name in _batch_templates(role):
            if not name or name not in gated:
                offenders.append(
                    f"{role.name}: {template} renders {name or '<unnamed>'}"
                )
    assert not offenders, (
        "Auto-deployable role(s) rendering an ungated batch workload. Name the CronJob in "
        "`flux_cronjob_gates` (docs/flux-deploy-plane.md). Or set "
        "k8s_autodeploy: false with a k8s_autodeploy_reason:\n  "
        + "\n  ".join(offenders)
    )


_GATED_CRONJOB_DEFAULTS = "flux_cronjob_gates:\n  widget-sync: {kind: verify}\n"


def test_a_flux_owned_role_is_credited_with_the_cronjobs_it_names_for_flux(
    widget_role,
) -> None:
    role = widget_role(defaults=_GATED_CRONJOB_DEFAULTS)
    assert flux_gated_names(role, owners={"widget": "flux"}) == {"widget-sync"}


def test_an_ansible_owned_role_gets_no_credit_for_the_flux_opt_in(widget_role) -> None:
    """Nothing renders the gate Job of a role Ansible still owns, so the opt-in gates nothing."""
    role = widget_role(defaults=_GATED_CRONJOB_DEFAULTS)
    assert flux_gated_names(role, owners={}) == set()


def test_pi_peer_backup_is_gated_by_its_flux_gate_alone() -> None:
    """The named member: its CronJob is credited through `flux_cronjob_gates` (#5652)."""
    role = _K8S_ROLES / "pi-peer-backup"
    assert flux_gated_names(role) == {"pi-peer-backup"}
