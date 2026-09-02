#!/usr/bin/env python3
"""Tests for the live-object drift check.

The comparison is a pure function over two dicts, which is what makes it testable at all:
the read-only ServiceAccount cannot write to the cluster, so drift cannot be induced live
from a session. Every "does it fire" test here is therefore a synthetic live/applied pair
rather than a real mutation — the mechanism is pure, so the injection point is the same one
main() feeds.

Run: uv run pytest ansible/roles/setup/k3s/tests/test_live_drift_check.py
"""

import copy
import pathlib
import re
import sys
import syslog

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "files"))
import live_drift_check as ldc  # noqa: E402


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


# ── the syslog line has to survive the host's own journald cap ──────────────────────────
#
# push()'s docstring says its syslog line is what makes this check visible to `probe.py
# alerts` and the Alert History board. That is only true if the level it logs at is one the
# host actually stores. It was not, for the up path, from creation until 2026-08-29.
#
# Derived from the Ansible source rather than hardcoded, so it goes RED from either side: if
# the check drops back to INFO, or if the host cap is tightened past NOTICE.

_SYSTEM_TUNING = (
    pathlib.Path(__file__).resolve().parents[2]
    / "initial_setup"
    / "tasks"
    / "system-tuning.yml"
)

# syslog severities: lower is more severe, and journald stores a line iff level <= the cap.
_SEVERITY = {
    "emerg": 0,
    "alert": 1,
    "crit": 2,
    "err": 3,
    "warning": 4,
    "notice": 5,
    "info": 6,
    "debug": 7,
}


def _journald_store_cap() -> int:
    """The MaxLevelStore initial_setup deploys, as a numeric severity."""
    text = _SYSTEM_TUNING.read_text()
    match = re.search(r"^\s*MaxLevelStore=(\w+)\s*$", text, re.MULTILINE)
    assert match, (
        f"no MaxLevelStore= in {_SYSTEM_TUNING} — the derivation lost its source"
    )
    return _SEVERITY[match.group(1)]


def test_the_cap_is_readable_from_the_ansible_source():
    # Without this, a moved file or a renamed key would make every assertion below vacuous
    # rather than failing — the corpus-went-empty trap.
    assert _journald_store_cap() == _SEVERITY["notice"]


def _level_push_uses(status: str, monkeypatch) -> int:
    """The severity push() actually hands to syslog for `status`.

    Calls the real push(). Asserting a literal here instead would be the guard-scope trap
    this whole file's newest tests exist to avoid: it would stay green if the code went back
    to LOG_INFO, because it would be testing the constant rather than the caller's choice.
    push() returns before any network work when PUSH_TOKEN/KUMA_HOST are unset.
    """
    seen: list[int] = []
    monkeypatch.setattr(ldc.syslog, "openlog", lambda **kwargs: None)
    monkeypatch.setattr(ldc.syslog, "closelog", lambda: None)
    monkeypatch.setattr(ldc.syslog, "syslog", lambda level, msg: seen.append(level))
    monkeypatch.delenv("PUSH_TOKEN", raising=False)
    monkeypatch.delenv("KUMA_HOST", raising=False)
    ldc.push(status, "message")
    assert len(seen) == 1, f"expected exactly one syslog line, got {len(seen)}"
    return seen[0]


def test_the_up_path_logs_at_a_level_this_host_stores(monkeypatch):
    assert _level_push_uses("up", monkeypatch) <= _journald_store_cap(), (
        "the up-path syslog line is logged below the host's MaxLevelStore, so it is dropped "
        "from the journal and from forwarding to rsyslog — the check beats its Kuma tile "
        "while leaving no positive record it ran"
    )


def test_the_down_path_logs_at_a_level_this_host_stores(monkeypatch):
    # The down line is what `probe.py alerts` matches on to reconstruct episodes.
    assert _level_push_uses("down", monkeypatch) <= _journald_store_cap()


def test_the_two_paths_are_not_the_same_level(monkeypatch):
    # down must outrank up, or the severity carries no information.
    assert _level_push_uses("down", monkeypatch) < _level_push_uses("up", monkeypatch)


def test_info_would_not_survive_this_cap():
    # The rejecting half: proves the assertions above are discriminating rather than trivially
    # true of every level. Reverting push() to LOG_INFO makes the up-path test fail, and this
    # is what says so. If this ever passes, the cap moved and that guard stopped testing.
    assert syslog.LOG_INFO > _journald_store_cap()
