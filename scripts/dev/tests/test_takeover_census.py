"""Tests for `takeover_census.py`, the operator's census before each H1 flip (H1.1j).

Each fixture pairs a live object with a dry-run apply of the render as kustomize-controller.
The dry-run body always carries every live field, as a real server-side dry run does, so a
census that read "declared" from the body rather than the Apply entry would list nothing.
"""

import copy
import io
import json

import pytest

import takeover_census as tc

RESTARTED_AT = "spec.template.metadata.annotations.kubectl.kubernetes.io/restartedAt"
LAST_APPLIED = "metadata.annotations.kubectl.kubernetes.io/last-applied-configuration"

REPLICAS = {"f:spec": {"f:replicas": {}}}
RESTART = {
    "f:spec": {
        "f:template": {
            "f:metadata": {"f:annotations": {"f:kubectl.kubernetes.io/restartedAt": {}}}
        }
    }
}


def entry(manager, operation, fields, subresource=None):
    out = {"manager": manager, "operation": operation, "fieldsV1": fields}
    if subresource:
        out["subresource"] = subresource
    return out


def deployment(*entries, kind="Deployment"):
    """A Deployment whose body holds every field any entry names, values elided."""
    return {
        "apiVersion": "apps/v1",
        "kind": kind,
        "metadata": {
            "name": "speedtest",
            "namespace": "homelab",
            "managedFields": list(entries),
        },
        "spec": {
            "replicas": 1,
            "template": {
                "metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": "x"}}
            },
        },
    }


def dry_run(live, declared):
    """The dry run of `live`: its body and entries, plus kustomize-controller's Apply."""
    out = copy.deepcopy(live)
    out["metadata"]["managedFields"].append(
        entry("kustomize-controller", "Apply", declared)
    )
    return out


def run(live, dryrun):
    return tc.census(tc.objects(live), tc.objects(dryrun))


def section(text, name):
    """The indented lines under the one heading that starts `  <name>`."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"  {name}"))
    out = []
    for line in lines[start + 1 :]:
        if not line.startswith("    "):
            break
        out.append(line.strip())
    return out


def test_a_restarted_at_the_render_omits_is_removed_and_rolls():
    live = deployment(
        entry("kubectl-client-side-apply", "Update", REPLICAS),
        entry("kubectl-rollout", "Update", RESTART),
    )
    text = run(live, dry_run(live, REPLICAS))
    assert section(text, "removed") == [f"{RESTARTED_AT}  (kubectl-rollout)"]
    assert "Deployment homelab/speedtest: rolls" in text.splitlines()


def test_a_pair_whose_managed_fields_match_lists_nothing():
    live = deployment(
        entry("kubectl-client-side-apply", "Update", REPLICAS),
        entry("kubectl-rollout", "Update", RESTART),
    )
    declared = {"f:spec": {**REPLICAS["f:spec"], **RESTART["f:spec"]}}
    text = run(live, dry_run(live, declared))
    assert section(text, "removed") == []
    assert section(text, "kept") == []
    assert "rolls" not in text


def test_a_field_a_manager_outside_the_cleanup_list_co_owns_is_not_removed():
    live = deployment(
        entry("kubectl-rollout", "Update", RESTART),
        entry("deploy@daniel-box", "Apply", RESTART),
    )
    text = run(live, dry_run(live, REPLICAS))
    assert section(text, "removed") == []
    assert section(text, "kept") == [RESTARTED_AT]
    assert "rolls" not in text


def test_a_field_only_an_outside_manager_owns_is_kept():
    live = deployment(entry("deploy@daniel-box", "Apply", REPLICAS))
    text = run(live, dry_run(live, {"f:metadata": {"f:labels": {"f:app": {}}}}))
    assert section(text, "kept") == ["spec.replicas"]
    assert section(text, "removed") == []


def test_a_kubectl_entry_on_a_subresource_is_ignored():
    """`kubectl scale` writes `spec.replicas` through `scale`, which matchFieldManager skips.

    The entry is the field's only owner and the render omits it, so only the subresource rule
    keeps it out of `removed`.
    """
    live = deployment(entry("kubectl-scale", "Update", REPLICAS, subresource="scale"))
    text = run(live, dry_run(live, {"f:metadata": {"f:labels": {"f:app": {}}}}))
    assert section(text, "removed") == []


def test_inputs_with_no_object_in_common_fail():
    live = deployment(entry("kubectl-rollout", "Update", RESTART))
    other = dry_run(live, REPLICAS)
    other["metadata"]["name"] = "something-else"
    with pytest.raises(tc.CensusError, match="share no object"):
        run(live, other)


def test_a_dry_run_not_applied_as_kustomize_controller_fails():
    live = deployment(entry("kubectl-rollout", "Update", RESTART))
    with pytest.raises(
        tc.CensusError, match="no dry-run object has a kustomize-controller Apply entry"
    ):
        run(live, copy.deepcopy(live))


def test_last_applied_is_removed_and_marked_expected():
    annotation = {
        "f:metadata": {
            "f:annotations": {"f:kubectl.kubernetes.io/last-applied-configuration": {}}
        }
    }
    live = deployment(entry("kubectl-client-side-apply", "Update", annotation))
    live["metadata"]["annotations"] = {
        "kubectl.kubernetes.io/last-applied-configuration": "{}"
    }
    text = run(live, dry_run(live, REPLICAS))
    assert section(text, "removed") == [
        f"{LAST_APPLIED}  (kubectl-client-side-apply, expected)"
    ]


def test_a_client_side_apply_server_default_prints_apart_and_does_not_roll():
    dns = {"f:spec": {"f:template": {"f:spec": {"f:dnsPolicy": {}}}}}
    live = deployment(entry("kubectl-client-side-apply", "Update", dns))
    live["metadata"]["annotations"] = {
        "kubectl.kubernetes.io/last-applied-configuration": json.dumps(
            {"spec": {"replicas": 1}}
        )
    }
    text = run(live, dry_run(live, REPLICAS))
    assert section(text, "removed") == []
    assert section(text, "presumed server defaults") == ["spec.template.spec.dnsPolicy"]
    assert "rolls" not in text


def test_main_reads_a_list_from_stdin_and_a_file(tmp_path, capsys):
    live = deployment(entry("kubectl-rollout", "Update", RESTART))
    dry = tmp_path / "dry.json"
    dry.write_text(json.dumps({"kind": "List", "items": [dry_run(live, REPLICAS)]}))
    stdin = io.StringIO(json.dumps({"kind": "List", "items": [live]}))
    assert tc.main(["-", str(dry)], stdin=stdin) == 0
    assert RESTARTED_AT in capsys.readouterr().out


def test_main_fails_without_echoing_an_unreadable_input(tmp_path, capsys):
    bad = tmp_path / "live.json"
    bad.write_text('{"data": {"password": "hunter2"')
    assert tc.main([str(bad), str(bad)]) == 1
    err = capsys.readouterr().err
    assert "not readable JSON" in err
    assert "hunter2" not in err
