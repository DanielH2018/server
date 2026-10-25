"""The `system-upgrade` Kustomization reads a Plan's status through three CEL expressions (H6.1b).

SUC's Plan status has no `observedGeneration`, so Flux cannot tell a status that predates the
apply from a current one. The expressions compare `status.latestVersion` with the pin as SUC
stores it, `+` replaced by `-`, and a missing field reads as stale. These tests evaluate the
rendered expressions against Plan fixtures with `_mini_cel`, a subset evaluator. The repo holds
no cel-go, so Flux's own evaluator is not run here: H6.1c's drill on the live cluster is the
proof of the reason strings and of the semantics this module assumes for an absent `status`.

Run: uv run pytest ansible/roles/k8s/system-upgrade/tests/test_plan_health_exprs.py
"""

import re
import shutil

import pytest
from _mini_cel import CelError, evaluate

from lib import yaml_fast
from lib.k8s_roles import K8S_ROLES, role_fields

PIN = "v1.37.1+k3s1"
STORED = "v1.37.1-k3s1"
STALE = "v1.36.9-k3s1"


def _exprs(roles=K8S_ROLES) -> dict[str, str]:
    fields, problems = role_fields(["system-upgrade"], roles)
    assert problems == []
    [check] = fields["system-upgrade"]["health_check_exprs"]
    assert (check["apiVersion"], check["kind"]) == ("upgrade.cattle.io/v1", "Plan")
    return check


def _state(check: dict[str, str], obj: dict) -> str:
    """Flux's order: `inProgress`, then `failed`, then `current`; none true reads InProgress."""
    if evaluate(check["inProgress"], obj):
        return "InProgress"
    if evaluate(check["failed"], obj):
        return "Failed"
    if evaluate(check["current"], obj):
        return "Current"
    return "InProgress"


def _plan(version=None, conditions=None, applying=None) -> dict:
    status: dict = {}
    if version is not None:
        status["latestVersion"] = version
    if conditions is not None:
        status["conditions"] = conditions
    if applying is not None:
        status["applying"] = applying
    return {"spec": {"version": PIN}, "status": status}


COMPLETE_TRUE = {"type": "Complete", "status": "True", "reason": "Complete"}
JOB_FAILED = {"type": "Complete", "status": "False", "reason": "JobFailed"}
RESOLVED = {"type": "Complete", "status": "False", "reason": "Resolved"}


def test_a_plan_with_no_status_reads_in_progress():
    assert _state(_exprs(), {"spec": {"version": PIN}}) == "InProgress"
    assert _state(_exprs(), _plan()) == "InProgress"


def test_complete_with_a_stale_version_reads_in_progress():
    # The red fixture for the stale-status race: a bumped Plan keeps the previous run's
    # Complete=True until SUC reconciles it.
    assert _state(_exprs(), _plan(STALE, [COMPLETE_TRUE])) == "InProgress"


def test_complete_in_sucs_munged_form_reads_current():
    assert _state(_exprs(), _plan(STORED, [COMPLETE_TRUE])) == "Current"


def test_the_unmunged_pin_never_matches_what_suc_stores():
    # SUC stores v1.37.1-k3s1. A literal built from the pin's own spelling would never be
    # Current, so every apply would time out red.
    assert _state(_exprs(), _plan(PIN, [COMPLETE_TRUE])) == "InProgress"


def test_a_stale_version_beside_an_old_job_failed_reads_in_progress_not_failed():
    assert _state(_exprs(), _plan(STALE, [JOB_FAILED])) == "InProgress"


def test_a_current_version_with_job_failed_reads_failed():
    assert _state(_exprs(), _plan(STORED, [JOB_FAILED], ["daniel-box"])) == "Failed"


def test_a_node_still_applying_reads_in_progress():
    assert _state(_exprs(), _plan(STORED, [RESOLVED], ["daniel-box"])) == "InProgress"
    # Complete=True with a node still listed as applying is the window before SUC clears it.
    assert (
        _state(_exprs(), _plan(STORED, [COMPLETE_TRUE], ["daniel-box"])) == "InProgress"
    )


def test_dropping_the_version_clause_would_read_a_stale_status_as_current():
    # The fixture's red proof: the `current` expression minus its version comparison accepts
    # the stale Complete=True that the real expression rejects.
    check = dict(_exprs())
    check["current"] = check["current"].split(" && has(status.latestVersion)")[0]
    assert "latestVersion" not in check["current"]
    assert (
        _state(check, _plan(STALE, [COMPLETE_TRUE])) == "InProgress"
    )  # inProgress catches it
    check["inProgress"] = "false"
    assert _state(check, _plan(STALE, [COMPLETE_TRUE])) == "Current"
    assert _state(_exprs(), _plan(STALE, [COMPLETE_TRUE])) == "InProgress"


def test_the_literal_follows_the_pin(tmp_path):
    roles = tmp_path / "roles"
    shutil.copytree(
        K8S_ROLES / "system-upgrade" / "defaults",
        roles / "system-upgrade" / "defaults",
    )
    defaults = roles / "system-upgrade" / "defaults" / "main.yml"
    text = defaults.read_text()
    assert f"k3s_version: {PIN}" in text
    defaults.write_text(
        text.replace(f"k3s_version: {PIN}", "k3s_version: v1.37.2+k3s1")
    )
    check = _exprs(roles)
    assert "v1.37.2-k3s1" in check["current"] and STORED not in check["current"]
    assert _state(check, _plan("v1.37.2-k3s1", [COMPLETE_TRUE])) == "Current"
    assert _state(check, _plan(STORED, [COMPLETE_TRUE])) == "InProgress"


def test_the_evaluator_refuses_what_it_does_not_implement():
    with pytest.raises(CelError):
        evaluate("status.conditions.all(c, c.status == 'True')", _plan(STORED, []))
    with pytest.raises(CelError):
        evaluate("status.latestVersion == 'x'", {})


def test_the_checked_in_defaults_name_each_expression():
    defaults = yaml_fast.safe_load(
        (K8S_ROLES / "system-upgrade" / "defaults" / "main.yml").read_text()
    )
    [check] = defaults["flux_health_check_exprs"]
    assert {"inProgress", "failed", "current"} <= set(check)


# Measured by the H6.1b2 rehearsal of `k3s-redproof` (run 20261024T193003Z on daniel-server,
# `--expect red --plan k3s-redproof`), copied from its rehearsal-verdict.txt. This is Flux's
# reading of the Plan. The evaluator here cannot produce it, so the tests below hold only the
# parts of it that the repo's own files determine.
OBSERVED_REASON = "HealthCheckFailed"
OBSERVED_MESSAGE = (
    "health check failed after 5m5.044785708s: failed early due to stalled resources: "
    "[Plan/system-upgrade/k3s-redproof status: 'Failed']"
)


# The drill Plan as it ran on 2026-10-25, reduced to what the tests below read. It is a fixture
# because the Plan was reverted after the drill, so no template renders it.
DRILL_PLAN = {
    "metadata": {"name": "k3s-redproof", "namespace": "system-upgrade"},
    "spec": {"jobActiveDeadlineSecs": 300},
}


def _drill() -> dict:
    return DRILL_PLAN


def test_the_observed_red_names_the_drill_plan_and_the_failed_expression():
    meta = _drill()["metadata"]
    assert OBSERVED_REASON == "HealthCheckFailed"
    assert (
        f"Plan/{meta['namespace']}/{meta['name']} status: 'Failed'" in OBSERVED_MESSAGE
    )
    # 'Failed' is the `failed` expression's result, which the drill's Plan status produces.
    assert _state(_exprs(), _plan(STORED, [JOB_FAILED], ["daniel-box"])) == "Failed"


def test_the_observed_red_arrived_at_the_drill_deadline_not_at_the_timeout():
    found = re.search(r"after (\d+)m([\d.]+)s", OBSERVED_MESSAGE)
    assert found is not None, "the observed message states the elapsed time"
    elapsed = int(found[1]) * 60 + float(found[2])
    deadline = _drill()["spec"]["jobActiveDeadlineSecs"]
    fields, problems = role_fields(["system-upgrade"], K8S_ROLES)
    assert problems == []
    timeout_minutes = int(fields["system-upgrade"]["timeout"].removesuffix("m"))
    # A red that arrives only at the Kustomization's timeout would prove the timeout (H6 step 4).
    assert deadline <= elapsed < deadline + 120
    assert elapsed < timeout_minutes * 60 / 10
