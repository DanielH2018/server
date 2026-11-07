"""The storage smoke test's verdict and its cleanup, against a fake cluster."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import storage_smoke
from lib.exit_codes import FAILED, OK
from lib.operator_kubectl import KubectlFailed


class Cluster:
    def __init__(self, phases=("Bound",), apply_fails=False, delete_fails=False):
        self.phases = iter(phases)
        self.last = ""
        self.apply_fails = apply_fails
        self.delete_fails = delete_fails
        self.calls = []
        self.applied: dict = {}

    def kubectl(self, *args, stdin=None):
        self.calls.append(args)
        if "apply" in args:
            if self.apply_fails:
                raise KubectlFailed("the webhook refused it")
            self.applied = json.loads(stdin or "")
            return ""
        if "delete" in args:
            if self.delete_fails:
                raise KubectlFailed("the API server went away")
            return ""
        phase = next(self.phases, self.last)
        self.last = phase
        if isinstance(phase, Exception):
            raise phase
        return phase


def smoke(cluster: Cluster) -> int:
    return storage_smoke.smoke(cluster.kubectl, sleep=lambda _: None)


def deleted(cluster: Cluster) -> bool:
    return any("delete" in call for call in cluster.calls)


def test_a_claim_that_binds_passes_and_is_deleted():
    cluster = Cluster(phases=("Pending", "Bound"))
    assert smoke(cluster) == OK
    assert cluster.applied["metadata"]["namespace"] == "default"
    assert cluster.applied["spec"]["storageClassName"] == "longhorn"
    assert deleted(cluster)


def test_a_claim_stuck_pending_fails_names_the_phase_and_is_deleted(capsys):
    cluster = Cluster(phases=("Pending",))
    assert smoke(cluster) == FAILED
    assert "reached Pending rather than Bound" in capsys.readouterr().err
    assert deleted(cluster)


def test_a_failed_read_is_retried_before_the_phase_decides():
    cluster = Cluster(phases=(KubectlFailed("not found yet"), "Bound"))
    assert smoke(cluster) == OK


def test_a_claim_that_never_appears_says_no_phase_at_all(capsys):
    cluster = Cluster(phases=(KubectlFailed("not found"),))
    assert smoke(cluster) == FAILED
    assert "no phase at all" in capsys.readouterr().err


def test_a_failed_create_fails_and_still_deletes(capsys):
    cluster = Cluster(apply_fails=True)
    assert smoke(cluster) == FAILED
    assert "the webhook refused it" in capsys.readouterr().err
    assert deleted(cluster)


def test_a_failed_delete_fails_a_bound_claim(capsys):
    cluster = Cluster(delete_fails=True)
    assert smoke(cluster) == FAILED
    assert "delete it by hand" in capsys.readouterr().err
