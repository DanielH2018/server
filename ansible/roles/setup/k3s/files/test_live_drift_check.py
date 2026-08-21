#!/usr/bin/env python3
"""Tests for the live-object drift check.

The comparison is a pure function over two dicts, which is what makes it testable at all:
the read-only ServiceAccount cannot write to the cluster, so drift cannot be induced live
from a session. Every "does it fire" test here is therefore a synthetic live/applied pair
rather than a real mutation — the mechanism is pure, so the injection point is the same one
main() feeds.

Run: uv run pytest ansible/roles/setup/k3s/files/test_live_drift_check.py
"""

import copy

import live_drift_check as ldc


def _applied(**spec) -> dict:
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "example", "namespace": "homelab"},
        "spec": spec,
    }


def _live(applied: dict, **overrides) -> dict:
    """A live object: the applied manifest plus the defaults and status a real one carries."""
    # Deep, not shallow: a shallow spec copy leaves nested dicts shared with `applied`, so a
    # test that mutates the live object mutates the baseline too and every diff comes back
    # empty. That failure looks exactly like the check not firing.
    live = copy.deepcopy(applied)
    live["spec"].update(overrides)
    live["status"] = {"replicas": 1, "readyReplicas": 1}
    live["metadata"] = {
        **applied["metadata"],
        "uid": "0000-1111",
        "resourceVersion": "12345",
        "managedFields": [{"manager": "kubectl"}],
        "annotations": {ldc.LAST_APPLIED: "{}"},
    }
    return live


# ── the check fires on real drift ────────────────────────────────────────────────────────


def test_a_changed_declared_field_is_drift():
    applied = _applied(replicas=1)
    diffs = ldc.subset_diff(_live(applied, replicas=3), applied)
    assert diffs == [(".spec.replicas", 3, 1)]


def test_a_removed_declared_field_is_drift():
    applied = _applied(replicas=1, paused=True)
    live = _live(applied)
    del live["spec"]["paused"]
    assert ldc.subset_diff(live, applied) == [(".spec.paused", "<absent>", True)]


def test_drift_inside_a_list_is_found_with_its_index():
    applied = _applied(
        template={"spec": {"containers": [{"name": "app", "image": "app:1.2"}]}}
    )
    live = _live(applied)
    live["spec"]["template"]["spec"]["containers"][0]["image"] = "app:1.3"
    assert ldc.subset_diff(live, applied) == [
        (".spec.template.spec.containers[0].image", "app:1.3", "app:1.2")
    ]


def test_a_list_that_grew_is_drift():
    # A sidecar added by hand. Compared whole rather than element-wise, because a length
    # change makes positional comparison meaningless.
    applied = _applied(template={"spec": {"containers": [{"name": "app"}]}})
    live = _live(applied)
    live["spec"]["template"]["spec"]["containers"].append({"name": "injected"})
    assert len(ldc.subset_diff(live, applied)) == 1


# ── and stays quiet on everything that is not drift ──────────────────────────────────────


def test_live_only_fields_are_not_drift():
    # The whole point of comparing a subset: status, uid, resourceVersion and managedFields
    # appear on every live object and are declared by no manifest.
    applied = _applied(replicas=1)
    assert ldc.subset_diff(_live(applied), applied) == []


def test_an_undeclared_field_changing_is_not_drift():
    # Nothing in the repo asserts a value for it, so there is nothing to have drifted from.
    applied = _applied(replicas=1)
    live = _live(applied, progressDeadlineSeconds=900)
    assert ldc.subset_diff(live, applied) == []


def test_canonicalised_quantities_are_not_drift():
    # The API server rewrites 1024Mi to 1Gi and 0.5 to 500m. This accounted for 24 of the
    # fleet's 25 apparent differences on the first run — every one of them a false positive.
    applied = _applied(
        template={
            "spec": {
                "containers": [
                    {"resources": {"limits": {"memory": "1024Mi", "cpu": "0.5"}}}
                ]
            }
        }
    )
    live = _live(applied)
    live["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"] = {
        "memory": "1Gi",
        "cpu": "500m",
    }
    assert ldc.subset_diff(live, applied) == []


def test_an_explicit_null_dropped_on_admission_is_not_drift():
    # longhorn-frontend's Service declares `nodePort: null`; the API server stores nothing.
    applied = _applied(ports=[{"port": 80, "nodePort": None}])
    live = _live(applied)
    live["spec"]["ports"] = [{"port": 80}]
    assert ldc.subset_diff(live, applied) == []


def test_a_real_null_mismatch_is_still_drift():
    # The null rule must not swallow a value that is present and wrong.
    applied = _applied(ports=[{"port": 80, "nodePort": None}])
    live = _live(applied)
    live["spec"]["ports"] = [{"port": 80, "nodePort": 31000}]
    assert ldc.subset_diff(live, applied) == [(".spec.ports[0].nodePort", 31000, None)]


# ── quantity parsing ─────────────────────────────────────────────────────────────────────


def test_quantities_parse_across_both_suffix_families():
    assert ldc.parse_quantity("1Gi") == ldc.parse_quantity("1024Mi")
    assert ldc.parse_quantity("500m") == ldc.parse_quantity("0.5")
    assert ldc.parse_quantity("1G") != ldc.parse_quantity("1Gi")  # decimal vs binary
    assert ldc.parse_quantity(2) == 2.0


def test_a_non_quantity_does_not_parse():
    # An image tag must never be read as a number — `nginx:1.29` and a bool both have to
    # fall through to plain equality, or two different images could compare equal.
    for value in ("nginx:1.29", "", "Always", None, True, False, {"a": 1}):
        assert ldc.parse_quantity(value) is None


# ── scope ────────────────────────────────────────────────────────────────────────────────


def test_foreign_namespaces_are_out_of_scope():
    assert ldc.is_foreign("deployment", "longhorn-system", "csi-attacher")
    assert ldc.is_foreign("configmap", "homelab", "kube-root-ca.crt")


def test_our_own_objects_are_in_scope():
    assert ldc.is_foreign("deployment", "homelab", "sonarr") is None


def test_the_patch_maintained_exemption_names_its_mechanism():
    # An exemption whose reason describes something adjacent to the risk is how a stale one
    # survives. This one has to name the writer and where it lives, so switching that task
    # to `apply` makes the exemption obviously wrong rather than permanently true.
    reason = ldc.is_foreign("configmap", "longhorn-system", "longhorn-storageclass")
    assert reason and "patch" in reason


# ── verdict ──────────────────────────────────────────────────────────────────────────────


def test_a_clean_run_is_zero():
    code, message = ldc.verdict([], ["a", "b"], [])
    assert code == 0
    assert "no drift" in message


def test_drift_is_a_failure_naming_the_objects():
    code, message = ldc.verdict([("deployment", "homelab", "sonarr", [])], [], [])
    assert code == 1
    assert "deployment homelab/sonarr" in message


def test_a_new_unannotated_object_is_a_failure():
    # At the floor it is silent; one above it is not. `kubectl apply` cannot prune removed
    # keys on an object with no baseline, which is the class that bit the static-monitors
    # Secret and monitor-bridge-env twice.
    at_floor = ["a"] * ldc.UNANNOTATED_FLOOR
    assert ldc.verdict([], at_floor, [])[0] == 0
    code, message = ldc.verdict([], at_floor + ["configmap homelab/new"], [])
    assert code == 1
    assert "configmap homelab/new" in message


def test_a_read_failure_is_not_a_clean_run():
    # Fail closed: a check that could not read the cluster must never report it clean.
    code, message = ldc.verdict([], [], ["deployment: connection refused"])
    assert code == 2
    assert "check failed" in message


def test_a_read_failure_outranks_a_clean_comparison():
    # Some kinds may have been read successfully. Reporting "no drift" off a partial read is
    # exactly the false green this ordering prevents.
    code, _ = ldc.verdict([], [], ["service: timeout"])
    assert code == 2
