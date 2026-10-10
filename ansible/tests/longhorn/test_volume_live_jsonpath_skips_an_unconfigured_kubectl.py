"""The two Longhorn seam tests skip, not fail, when `kubectl` exists but can load no config (#4221).

As the `claude` agent user on daniel-box, `kubectl` is on PATH but falls back to
`/etc/rancher/k3s/k3s.yaml`, which only root reads. That is "no cluster to ask", the same as a
missing binary, and must skip the same way. A stub `kubectl` that fails every call with the
recorded stderr stands in for that client, so the case is reproduced on any machine.

Each seam test runs in a child pytest, and its JUnit testcase is the oracle. A skip written as
a `skipif` marker or a fixture reaches pytest's report but never a plain function call.
"""

import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from _helpers import REPO
from lib.proc_testing import fake_bin, run

# Recorded 2026-10-10 as `claude` on daniel-box: the k3s wrapper's warning, then kubectl's error.
_UNREADABLE_KUBECONFIG = (
    'time="2026-10-10T15:02:24Z" level=warning msg="Unable to read /etc/rancher/k3s/k3s.yaml, '
    "please start server with --write-kubeconfig-mode or --write-kubeconfig-group to modify kube "
    'config permissions"\n'
    'error: error loading config file "/etc/rancher/k3s/k3s.yaml": open '
    "/etc/rancher/k3s/k3s.yaml: permission denied\n"
)


def _seam_outcome(node: str, stub: Path, tmp_path: Path) -> ET.Element:
    """Run one seam test in a child pytest with `stub` first on PATH, and return its testcase."""
    report = tmp_path / "report.xml"
    env = {k: v for k, v in os.environ.items() if k != "KUBECONFIG"}
    run(
        [
            sys.executable,
            "-m",
            "pytest",
            node,
            "-n0",
            "-p",
            "no:cacheprovider",
            f"--junitxml={report}",
        ],
        cwd=REPO,
        env=env,
        stub_bin=stub,
        timeout=120,
    )
    cases = ET.parse(report).getroot().findall(".//testcase")
    assert len(cases) == 1, f"{node} matched {len(cases)} tests"
    return cases[0]


def _assert_skipped(case: ET.Element) -> None:
    failure = case.find("failure")
    detail = (
        failure.get("message") if failure is not None else ET.tostring(case, "unicode")
    )
    assert case.find("skipped") is not None, (
        f"the seam test did not skip a kubectl that can load no config: {detail}"
    )


@pytest.fixture
def unconfigured_kubectl(tmp_path) -> Path:
    """A `kubectl` that fails every call, `config view` included, for want of a readable
    kubeconfig."""
    return fake_bin(
        tmp_path / "bin",
        kubectl=f"cat >&2 <<'STDERR'\n{_UNREADABLE_KUBECONFIG}STDERR\nexit 1\n",
    )


def test_the_snapshot_listing_seam_skips_an_unreadable_kubeconfig(
    unconfigured_kubectl, tmp_path
) -> None:
    _assert_skipped(
        _seam_outcome(
            "ansible/tests/longhorn/test_volume_snapshot.py::test_the_listing_jsonpath_parses",
            unconfigured_kubectl,
            tmp_path,
        )
    )


def test_the_revert_listing_seam_skips_an_unreadable_kubeconfig(
    unconfigured_kubectl, tmp_path
) -> None:
    _assert_skipped(
        _seam_outcome(
            "ansible/tests/longhorn/test_volume_revert.py::test_the_listing_jsonpath_parses",
            unconfigured_kubectl,
            tmp_path,
        )
    )
