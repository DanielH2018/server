"""A role `scripts/dev/new_k8s_service.py` scaffolds is gated on the Deployment it ships.

The scaffolder writes a bare `k8s/manifests` include with no `manifests_rollout` (#4298), so
the gate waits on the role's own name. These tests run the rollout-gate guards' resolver over a
scaffolded role rather than restating its regex.
"""

from pathlib import Path

import new_k8s_service as scaffold
from _autodeploy import _deployment_templates
from _autodeploy_rollout import _primary_rollout_name, _ungated_deployments


def _scaffolded_widget(tmp_path: Path) -> Path:
    args = scaffold.parse_args(["widget", "--image", "img:1", "--port", "8080"])
    scaffold.write_role(args, tmp_path)
    return tmp_path / "widget"


def test_a_scaffolded_role_gates_the_deployment_it_ships(tmp_path: Path) -> None:
    """The scaffolder's bare include waits on the one Deployment it writes (#4298).

    With no `manifests_rollout`, the gate's target is the role's name, so the scaffolded
    Deployment must carry that name.
    """
    role = _scaffolded_widget(tmp_path)
    assert _primary_rollout_name(role) == "widget"
    assert _deployment_templates(role) == ["deployment.yaml.j2"]
    assert _ungated_deployments(role) == []


def test_a_scaffolded_deployment_under_another_name_reads_as_ungated(
    tmp_path: Path,
) -> None:
    """Control: the check above goes red when the Deployment and the role name disagree."""
    role = _scaffolded_widget(tmp_path)
    args = scaffold.parse_args(["widget", "--image", "img:1", "--port", "8080"])
    (role / "templates" / "deployment.yaml.j2").write_text(
        scaffold.deployment_template("widget-app", args.strategy, args.priority_class)
    )
    assert _ungated_deployments(role) == ["deployment.yaml.j2"]
