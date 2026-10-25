"""The `binary` gate, `k3s_binary_gate` (H6.3e): a node's k3s binary against the release's sum."""

import hashlib

import pytest
from _k3s_gates_fixtures import fake_get, healthy, listing, node

import k3s_binary_gate
import k3s_gates

BINARY = b"fixture k3s binary"
SUM = hashlib.sha256(BINARY).hexdigest()
VERSION = "v1.37.1+k3s1"


@pytest.fixture
def root(tmp_path):
    (tmp_path / "usr/local/bin").mkdir(parents=True)
    (tmp_path / "usr/local/bin/k3s").write_bytes(BINARY)
    return str(tmp_path)


def _routes(kubelet=VERSION):
    return healthy() | {
        k3s_gates.NODES_PATH: listing(node("daniel-box", version=kubelet))
    }


def _run(root, kubelet=VERSION, sha=SUM, node_name="daniel-box"):
    notes: list[str] = []
    get = fake_get(_routes(kubelet))
    return k3s_binary_gate.check_binary(
        get, root, node_name, VERSION, sha, notes
    ), notes


def test_a_matching_binary_passes(root):
    assert _run(root) == ([], [])


def test_a_mismatched_binary_names_the_node_and_both_sums(root):
    refusals, _ = _run(root, sha="0" * 64)
    assert len(refusals) == 1
    assert "daniel-box" in refusals[0] and SUM in refusals[0]


def test_a_node_at_another_version_is_pending_not_failed(root):
    assert _run(root, kubelet="v1.37.0+k3s1", sha="0" * 64) == (
        [],
        ["node daniel-box runs v1.37.0+k3s1, not v1.37.1+k3s1: pending"],
    )


def test_an_absent_node_and_a_missing_binary_refuse(root, tmp_path):
    assert _run(root, node_name="ghost")[0]
    assert "cannot read" in _run(str(tmp_path / "empty"))[0][0]


def test_main_exits_zero_on_a_match_and_one_on_a_mismatch(root):
    argv = ["--host-root", root, "--node", "daniel-box", "--expect-version", VERSION]
    get = fake_get(_routes())
    assert k3s_binary_gate.main([*argv, "--expect-sha256", SUM], get) == 0
    assert k3s_binary_gate.main([*argv, "--expect-sha256", "0" * 64], get) == 1
