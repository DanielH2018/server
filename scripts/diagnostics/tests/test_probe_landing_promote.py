"""`probe.py landing`'s promotion lines: both holds, then `prod` and `last-good`.

The three reads are fakes handed to `collect`, so no test reaches GitHub, GHCR or the cluster.

Run: uv run pytest scripts/diagnostics/tests/test_probe_landing_promote.py
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from diagnostics.probe_lib import landing_blockers as landing
from diagnostics.probe_lib import promote_state
from diagnostics.probe_lib.promote_state import (
    PromoteReads,
    artifact_of,
    hold_lines,
    reconcile_disabled_objects,
    suspended_objects,
)
from test_probe_landing import CLEAR, GREEN, fake_deploy_ui, fake_run

PROD = {"commit": "c" * 40, "digest": "sha256:" + "1" * 64}
OTHER = {"commit": "d" * 40, "digest": "sha256:" + "2" * 64}
HELD = {"hold_sha": "f" * 40, "hold_plane_entries": []}


def _broken():
    raise TimeoutError("the read timed out")


def _read(value):
    """`value` as a read: a callable stays one, and anything else is returned as it is."""
    return value if callable(value) else lambda: value


def _snap(
    deployer=CLEAR,
    deploy_hold="none",
    prod=PROD,
    last_good=PROD,
    suspended=None,
    reconcile_disabled=None,
) -> dict:
    reads = PromoteReads(
        _read(deploy_hold),
        _read(prod),
        _read(last_good),
        None if suspended is None else _read(suspended),
        None if reconcile_disabled is None else _read(reconcile_disabled),
    )
    return landing.collect(
        lambda: None, fake_deploy_ui(deployer), fake_run(GREEN), promote=reads
    )


def _lines(snap: dict) -> dict[str, str]:
    """Each promotion line by the words before its colon."""
    return {line.split(":", 1)[0]: line for line in hold_lines(snap)}


HOLD_SHA = "hold_sha (GitOps deployer, Ansible-owned tags)"
DEPLOY_HOLD = "DEPLOY_HOLD (promote workflow, Flux-owned tags)"


@pytest.mark.parametrize(
    "deployer, deploy_hold, want_sha, want_hold",
    [
        (HELD, f"{'e' * 40} failed=bento-pdf", "ffffffff", "failed=bento-pdf"),
        (HELD, "none", "ffffffff", ": none"),
        (CLEAR, "none", ": none", ": none"),
    ],
    ids=["both-set", "one-set", "both-none"],
)
def test_both_hold_lines_print_whatever_each_reads(
    deployer, deploy_hold, want_sha, want_hold
):
    lines = _lines(_snap(deployer, deploy_hold))
    assert want_sha in lines[HOLD_SHA]
    assert lines[DEPLOY_HOLD].endswith(want_hold)
    text = landing.format_text({**_snap(deployer, deploy_hold), "claims": None})
    assert lines[HOLD_SHA] in text and lines[DEPLOY_HOLD] in text


@pytest.mark.parametrize(
    "deploy_hold, cause",
    [
        (f"{'e' * 40} suspended-changed=bento-pdf", "cause: suspended-changed"),
        (f"{'e' * 40} failed=bento-pdf", ""),
        (f"{'e' * 40} stateful=sonarr", "cause: stateful, sonarr failed"),
        (f"{'e' * 40} stateful=unverified", "cause: stateful, verify left no verdict"),
    ],
    ids=["suspended-changed", "failed", "stateful", "stateful-unverified"],
)
def test_a_suspended_changed_hold_prints_its_cause(deploy_hold, cause):
    line = _lines(_snap(CLEAR, deploy_hold))[DEPLOY_HOLD]
    assert line.startswith(f"{DEPLOY_HOLD}: {deploy_hold}")
    assert ("cause:" in line) == bool(cause) and cause in line


def test_both_hold_lines_print_when_neither_hold_could_be_read():
    """A line never goes missing: an unread hold prints `unknown`, not nothing."""
    snap = {**_snap(), "hold": None, "promote": None, "claims": None}
    lines = _lines(snap)
    assert lines[HOLD_SHA].endswith(": unknown")
    assert lines[DEPLOY_HOLD].endswith(": unknown")
    assert lines[HOLD_SHA] in landing.format_text(snap)


def test_prod_equal_to_last_good_is_reported_when_both_digests_match():
    assert "last-good: the same digest as prod" in hold_lines(_snap())[2]


def test_a_failed_last_good_read_reads_unknown_and_never_equal_to_prod():
    snap = _snap(last_good=_broken)
    line = hold_lines(snap)[2]
    assert line.endswith("last-good: unknown")
    assert "same digest" not in line
    assert any(e.startswith("last_good: TimeoutError") for e in snap["errors"])
    assert snap["promote"]["deploy_hold"] == "none", "one failed read leaves the others"


def test_a_last_good_not_yet_seeded_reads_none():
    assert hold_lines(_snap(last_good=None))[2].endswith("last-good: none")


def test_a_last_good_on_another_digest_names_it():
    line = hold_lines(_snap(last_good=OTHER))[2]
    assert line.endswith(f"last-good: dddddddd {OTHER['digest'][:19]}")


def test_a_failed_deploy_hold_read_reads_unknown():
    assert _lines(_snap(deploy_hold=_broken))[DEPLOY_HOLD].endswith(": unknown")


def test_collect_without_the_promotion_reads_reads_none_of_them():
    snap = landing.collect(lambda: None, fake_deploy_ui(CLEAR), fake_run(GREEN))
    assert snap["promote"] == {
        "deploy_hold": None,
        "prod": None,
        "last_good": None,
        "suspended": None,
        "reconcile_disabled": None,
    }


def test_prod_comes_from_the_ocirepositorys_artifact_revision_and_metadata():
    artifact = {
        "revision": f"prod@{PROD['digest']}",
        "metadata": {
            "org.opencontainers.image.revision": f"master@sha1:{PROD['commit']}"
        },
    }
    assert artifact_of({"status": {"artifact": artifact}}) == PROD


def test_an_ocirepository_without_a_revision_is_an_error():
    with pytest.raises(ValueError, match=r"status\.artifact"):
        artifact_of({"status": {"artifact": {"revision": f"prod@{PROD['digest']}"}}})


def _flux_object(kind: str, name: str, suspend, manager: str) -> dict:
    """A Flux object as `kubectl get -o json --show-managed-fields` returns it."""
    fields = {"f:spec": {"f:suspend": {}}} if suspend is not None else {"f:spec": {}}
    spec = {} if suspend is None else {"suspend": suspend}
    return {
        "kind": kind,
        "metadata": {
            "name": name,
            "managedFields": [
                {"manager": "gotk-kustomize-controller", "fieldsV1": {"f:spec": {}}},
                {"manager": manager, "fieldsV1": fields},
            ],
        },
        "spec": spec,
    }


def test_a_hand_suspend_and_a_git_suspend_print_their_field_managers():
    items = [
        _flux_object("Kustomization", "sonarr", True, "homelab-operator"),
        _flux_object("Kustomization", "radarr", True, "kustomize-controller"),
        _flux_object("Kustomization", "texbrain", None, "kustomize-controller"),
        _flux_object("OCIRepository", "homelab", False, "homelab-operator"),
    ]
    line = hold_lines(_snap(suspended=suspended_objects(items)))[3]
    assert line == (
        "suspended (Flux): Kustomization/sonarr by homelab-operator; "
        "Kustomization/radarr by kustomize-controller"
    )


def test_no_suspended_object_reads_none_and_an_unread_one_reads_unknown():
    assert hold_lines(_snap(suspended=[]))[3] == "suspended (Flux): none"
    snap = _snap(suspended=_broken)
    assert hold_lines(snap)[3] == "suspended (Flux): unknown"
    assert any(e.startswith("suspended: TimeoutError") for e in snap["errors"])


def _member(kind: str, name: str, annotations: dict | None) -> dict:
    """A named member as `kubectl get -o json` returns it."""
    return {
        "kind": kind,
        "metadata": {
            "name": name,
            "namespace": "kube-system",
            **({} if annotations is None else {"annotations": annotations}),
        },
    }


DISABLED = {"kustomize.toolkit.fluxcd.io/reconcile": "disabled"}


def test_an_annotated_coredns_member_is_named_and_an_unannotated_one_is_not():
    items = [
        _member("Deployment", "coredns", DISABLED),
        _member("ConfigMap", "coredns", {"other": "disabled"}),
        _member("Service", "kube-dns", None),
    ]
    line = hold_lines(_snap(reconcile_disabled=reconcile_disabled_objects(items)))[4]
    assert line == "reconcile-disabled (Flux skips): Deployment/coredns in kube-system"


def test_no_annotated_member_reads_none_and_an_unread_one_reads_unknown():
    assert hold_lines(_snap(reconcile_disabled=[]))[4] == (
        "reconcile-disabled (Flux skips): none"
    )
    snap = _snap(reconcile_disabled=_broken)
    assert hold_lines(snap)[4] == "reconcile-disabled (Flux skips): unknown"
    assert any(e.startswith("reconcile_disabled: TimeoutError") for e in snap["errors"])


def test_the_live_read_covers_the_coredns_objects_and_both_backup_targets():
    import json
    import subprocess

    calls = []
    state = {"missing": None}

    def fake(cluster, *args, **kwargs):
        calls.append(args)
        if state["missing"] and state["missing"] in args:
            return subprocess.CompletedProcess(args, 1, "", "NotFound")
        if "longhorn-system" in args:
            target = _member("BackupTarget", "default", DISABLED)
            target["metadata"]["namespace"] = "longhorn-system"
            items = [target, _member("BackupTarget", "r2", None)]
        else:
            items = [_member("Deployment", "coredns", DISABLED)]
        return subprocess.CompletedProcess(args, 0, json.dumps({"items": items}), "")

    assert promote_state._reconcile_disabled(fake) == [
        {"kind": "Deployment", "namespace": "kube-system", "name": "coredns"},
        {"kind": "BackupTarget", "namespace": "longhorn-system", "name": "default"},
    ]
    assert calls[0][1:4] == (
        "deployment/coredns",
        "configmap/coredns",
        "service/kube-dns",
    )
    assert calls[1][1:3] == (
        "backuptargets.longhorn.io/default",
        "backuptargets.longhorn.io/r2",
    )
    # RED: a named member that cannot be read must not read as "no annotation".
    for member in ("service/kube-dns", "backuptargets.longhorn.io/r2"):
        state["missing"] = member
        with pytest.raises(RuntimeError, match="NotFound"):
            promote_state._reconcile_disabled(fake)
