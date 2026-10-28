"""Tests for `probe.py flux` and the `cc-wait flux` source (`flux_wait.py`).

The fixtures carry the fields a live `kubectl get kustomizations,ocirepositories -o json` held
on 2026-10-25 mid-promotion: the OCIRepository's commit under
`status.artifact.metadata["org.opencontainers.image.revision"]`, each Kustomization's under
`status.lastAppliedOriginRevision`.
"""

import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import flux_wait
import pytest
from diagnostics.probe_lib import flux
from lib.exit_codes import PROBE_UNREADABLE
from renderer.renderer_lib.ordering import TAG_KIND_SUFFIXES

OLD = "613ab5028cd4974a6f0247400e58749705719a72"
NEW = "86d0ba5cae25b3a595cfa69ce5d29dd07460e56b"
ARTIFACT = "prod@sha256:" + "a" * 64


def _source(commit=NEW, name="homelab"):
    return {
        "kind": "OCIRepository",
        "metadata": {"name": name},
        "status": {
            "artifact": {
                "revision": ARTIFACT,
                "metadata": {
                    "org.opencontainers.image.revision": f"master@sha1:{commit}"
                },
            }
        },
    }


def _ks(name, ready="True", reason="ReconciliationSucceeded", applied=NEW, **kw):
    return {
        "kind": "Kustomization",
        "metadata": {"name": name, "generation": 4},
        "spec": {
            "sourceRef": {"name": "homelab"},
            "suspend": kw.get("suspend", False),
            "dependsOn": [{"name": d} for d in kw.get("depends_on", ())],
        },
        "status": {
            "observedGeneration": kw.get("observed", 4),
            "lastAppliedOriginRevision": f"master@sha1:{applied}",
            "lastAttemptedRevision": kw.get("attempted", ARTIFACT),
            "conditions": [
                {"type": "Ready", "status": ready, "reason": reason, "message": "m"}
            ],
        },
    }


def test_the_suffixes_match_the_renderers():
    assert flux.TAG_KIND_SUFFIXES == TAG_KIND_SUFFIXES


def test_a_tag_selects_its_per_tag_kinds_and_soak_and_no_other_tag():
    names = ["sonarr", "sonarr-verify", "sonarr-snapshot", "sonarrx", "radarr", "soak"]
    chosen, missing = flux.select(names, ["sonarr"])
    assert chosen == ["sonarr", "sonarr-snapshot", "sonarr-verify", "soak"]
    assert missing == []


def test_settled_rows_are_clean():
    items = [_source(), _ks("sonarr"), _ks("sonarr-verify"), _ks("soak")]
    text, code = flux.format_flux(items, ["sonarr"])
    assert code == 0, text
    assert "OK: all 3 Kustomizations" in text


def test_a_row_behind_its_source_is_flagged():
    items = [_source(), _ks("sonarr", applied=OLD)]
    text, code = flux.format_flux(items, ["sonarr"])
    assert code == 1
    assert "sonarr: applied 613ab502, but homelab serves 86d0ba5c" in text


def test_a_failed_verify_sibling_is_flagged():
    items = [
        _source(),
        _ks("sonarr"),
        _ks("sonarr-verify", "False", "HealthCheckFailed"),
    ]
    text, code = flux.format_flux(items, ["sonarr"])
    assert code == 1
    assert "sonarr-verify: Ready=False HealthCheckFailed" in text


def test_a_reconciling_row_is_pending_not_failed():
    """Flux's in-flight state, seen live on qbittorrent on 2026-10-25 (#6337)."""
    items = [
        _source(),
        _ks("sonarr"),
        _ks("sonarr-verify", "Unknown", "Progressing", applied=OLD),
    ]
    text, code = flux.format_flux(items, ["sonarr"])
    assert code == 0, text
    assert "FAIL:" not in text
    assert (
        "PENDING (reconciling, not failed):\n  sonarr-verify: Ready=Unknown Progressing"
        in text
    )
    assert "OK: 1 of 2 Kustomizations are Ready" in text


def test_a_sibling_waiting_on_its_reconciling_tag_is_pending():
    """`-verify` dependsOn the tag, so Flux holds it at DependencyNotReady meanwhile."""
    items = [
        _source(),
        _ks("sonarr", "Unknown", "Progressing", applied=OLD),
        _ks("sonarr-verify", "False", "DependencyNotReady", applied=OLD),
    ]
    text, code = flux.format_flux(items, ["sonarr"])
    assert code == 0, text
    assert "sonarr-verify: Ready=False DependencyNotReady" in text


def test_a_row_waiting_on_a_failed_dependency_fails():
    """configarr dependsOn sonarr, which no select() of `configarr` prints (#6340)."""
    items = [
        _source(),
        _ks("sonarr", "False", "HealthCheckFailed"),
        _ks("configarr", "False", "DependencyNotReady", depends_on=["sonarr"]),
    ]
    text, code = flux.format_flux(items, ["configarr"])
    assert code == 1, text
    assert "FAIL:\n  configarr: Ready=False DependencyNotReady" in text
    assert "dependency sonarr failed" in text


def test_a_row_waiting_on_a_reconciling_dependency_stays_pending():
    items = [
        _source(),
        _ks("sonarr", "Unknown", "Progressing", applied=OLD),
        _ks("configarr", "False", "DependencyNotReady", depends_on=["sonarr"]),
    ]
    text, code = flux.format_flux(items, ["configarr"])
    assert code == 0, text


def test_a_row_with_no_ready_condition_fails():
    """A stopped kustomize-controller leaves a never-reconciled object with no conditions."""
    item = _ks("sonarr")
    item["status"]["conditions"] = []
    text, code = flux.format_flux([_source(), item], ["sonarr"])
    assert code == 1
    assert "FAIL:\n  sonarr: Ready=Unknown" in text


def test_a_failed_row_beside_a_reconciling_one_still_fails():
    items = [
        _source(),
        _ks("sonarr", "Unknown", "Progressing"),
        _ks("sonarr-verify", "False", "HealthCheckFailed"),
    ]
    text, code = flux.format_flux(items, ["sonarr"])
    assert code == 1
    assert "  sonarr: Ready=Unknown Progressing" in text.split("FAIL:")[0]
    assert "sonarr-verify: Ready=False HealthCheckFailed" in text.split("FAIL:")[1]


def test_a_suspended_row_is_flagged():
    text, code = flux.format_flux([_source(), _ks("sonarr", suspend=True)], ["sonarr"])
    assert code == 1
    assert "sonarr: suspended" in text


def test_a_tag_with_no_kustomization_is_flagged():
    text, code = flux.format_flux([_source(), _ks("sonarr")], ["immich"])
    assert code == 1
    assert "immich: no Kustomization" in text


def test_a_list_with_no_kustomization_is_unreadable():
    def read(cluster, *args):
        return {"items": [_source()]}

    with pytest.raises(flux.FluxUnreadable, match="no Kustomization"):
        flux.read_flux(read=read)


def _failed_read(cluster, *args):
    return None


def _timed_out_read(cluster, *args):
    raise subprocess.TimeoutExpired(["kubectl", *args], 60.0)


def test_run_flux_exits_unreadable_on_a_kubectl_timeout(capsys):
    """A stalled API server is a failed read (75), never the `fail` verdict."""
    ns = type("NS", (), {"tags": [], "dry_run": False, "cluster": "prod"})()
    assert flux.run_flux(ns, read=_timed_out_read) == PROBE_UNREADABLE
    assert "timed out after 60.0s" in capsys.readouterr().err


def test_run_flux_exits_unreadable_on_a_failed_read(capsys):
    ns = type("NS", (), {"tags": [], "dry_run": False, "cluster": "prod"})()
    assert flux.run_flux(ns, read=_failed_read) == PROBE_UNREADABLE
    assert "flux:" in capsys.readouterr().err


# -- the cc-wait source -------------------------------------------------------------------


def test_wait_is_ready_at_the_commit_the_source_serves():
    state = flux.wait_state([_source(), _ks("sonarr")], "sonarr", None)
    assert state["state"] == "ready"


def test_wait_stays_open_while_behind_or_on_a_dependency():
    items = [_source(), _ks("sonarr", "False", "DependencyNotReady", applied=OLD)]
    assert flux.wait_state(items, "sonarr", None)["state"] == "progressing"


def test_wait_stays_open_on_a_ready_kustomization_that_has_not_observed_its_spec():
    items = [_source(), _ks("sonarr", observed=3)]
    assert flux.wait_state(items, "sonarr", None)["state"] == "progressing"


def test_wait_fails_on_a_failed_apply_of_the_expected_commit():
    items = [_source(), _ks("sonarr", "False", "HealthCheckFailed", applied=OLD)]
    state = flux.wait_state(items, "sonarr", NEW[:7])
    assert state["state"] == "failed"
    assert "HealthCheckFailed" in state["detail"]


def test_wait_does_not_fail_on_a_failure_at_another_commit():
    """The source still serves OLD, so a failed apply says nothing about NEW."""
    items = [
        _source(OLD),
        _ks("sonarr", "False", "HealthCheckFailed", applied=OLD),
    ]
    assert flux.wait_state(items, "sonarr", NEW)["state"] == "progressing"


def test_wait_does_not_fail_before_the_source_artifact_was_attempted():
    items = [
        _source(),
        _ks(
            "sonarr",
            "False",
            "HealthCheckFailed",
            applied=OLD,
            attempted="prod@sha256:b",
        ),
    ]
    assert flux.wait_state(items, "sonarr", None)["state"] == "progressing"


def test_wait_fails_on_a_dependency_that_failed():
    """Flux holds configarr at DependencyNotReady for good once sonarr fails (#6354)."""
    items = [
        _source(),
        _ks("sonarr", "False", "HealthCheckFailed"),
        _ks("configarr", "False", "DependencyNotReady", depends_on=["sonarr"]),
    ]
    state = flux.wait_state(items, "configarr", None)
    assert state["state"] == "failed"
    assert "sonarr" in state["detail"]


def test_wait_fails_on_a_dependency_held_by_a_failed_one():
    items = [
        _source(),
        _ks("sonarr", "False", "HealthCheckFailed"),
        _ks("sonarr-verify", "False", "DependencyNotReady", depends_on=["sonarr"]),
        _ks("configarr", "False", "DependencyNotReady", depends_on=["sonarr-verify"]),
    ]
    assert flux.wait_state(items, "configarr", None)["state"] == "failed"


def test_wait_does_not_fail_on_a_dependency_failure_at_an_older_artifact():
    """sonarr's failure is about the artifact before the one its source serves now."""
    items = [
        _source(),
        _ks("sonarr", "False", "HealthCheckFailed", attempted="prod@sha256:b"),
        _ks("configarr", "False", "DependencyNotReady", depends_on=["sonarr"]),
    ]
    assert flux.wait_state(items, "configarr", None)["state"] == "progressing"


def test_wait_is_ready_at_a_later_commit_that_holds_the_expected_one():
    """Another session's promotion landed before Flux applied OLD, and NEW carries it."""
    items = [_source(), _ks("sonarr")]
    state = flux.wait_state(
        items, "sonarr", OLD, contains=lambda c, a: (c, a) == (NEW, OLD)
    )
    assert state["state"] == "ready"


def test_wait_without_a_revision_stays_open_until_a_reset_to_an_older_commit_applies():
    """Master was reset to OLD, so the NEW still applied descends from it but is stale (#6336)."""
    items = [_source(OLD), _ks("sonarr")]
    state = flux.wait_state(
        items, "sonarr", None, contains=lambda c, a: (c, a) == (NEW, OLD)
    )
    assert state["state"] == "progressing"


def test_wait_stays_open_at_a_later_commit_that_does_not_hold_the_expected_one():
    items = [_source(), _ks("sonarr")]
    state = flux.wait_state(items, "sonarr", OLD, contains=lambda c, a: False)
    assert state["state"] == "progressing"


def test_wait_ends_on_a_suspended_kustomization():
    items = [_source(), _ks("sonarr", suspend=True)]
    assert flux.wait_state(items, "sonarr", None)["state"] == "suspended"


def test_wait_states_map_to_the_terminal_codes():
    assert flux_wait.describe()["terminal"] == {"ready": 0, "failed": 1, "suspended": 3}


def test_flux_wait_exits_nonzero_on_a_failed_read(capsys):
    """cc-wait retries a probe that exits non-zero, rather than reading a state."""
    assert flux_wait.main(["sonarr"], read=_failed_read) == 1
    assert capsys.readouterr().out == ""


def test_flux_wait_reports_a_kubectl_timeout_on_one_line(capsys):
    assert flux_wait.main(["sonarr"], read=_timed_out_read) == 1
    err = capsys.readouterr().err
    assert err.startswith("flux_wait: kubectl timed out") and "Traceback" not in err


def test_flux_wait_prints_one_reading(capsys):
    def read(cluster, *args):
        return {"items": [_source(), _ks("sonarr")]}

    assert flux_wait.main(["sonarr"], read=read) == 0
    assert '"state": "ready"' in capsys.readouterr().out
