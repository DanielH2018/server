"""The cronjob-gate seam test skips, not fails, when `kubectl` exists but can load no config (#4221).

As the `claude` agent user on daniel-box, `kubectl` is on PATH but falls back to
`/etc/rancher/k3s/k3s.yaml`, which only root reads. That is "no cluster to ask", the same as a
missing binary, and must skip the same way. A stub `kubectl` that fails every call with the
recorded stderr stands in for that client, so the case is reproduced on any machine.
`test_cronjob_gate_decision.py` sits at its module-length ceiling, so this lives beside it.

The seam test runs in a child pytest, and its JUnit testcase is the oracle. A skip written as a
`skipif` marker or a fixture reaches pytest's report but never a plain function call.
"""

import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from _helpers import REPO
from lib.proc_testing import fake_bin, run

_SEAM = (
    "ansible/tests/deploy/test_cronjob_gate_decision.py"
    "::test_the_jsonpath_parses_against_the_live_api"
)
# Recorded 2026-10-10 as `claude` on daniel-box: the k3s wrapper's warning, then kubectl's error.
_UNREADABLE_KUBECONFIG = (
    'time="2026-10-10T15:02:24Z" level=warning msg="Unable to read /etc/rancher/k3s/k3s.yaml, '
    "please start server with --write-kubeconfig-mode or --write-kubeconfig-group to modify kube "
    'config permissions"\n'
    'error: error loading config file "/etc/rancher/k3s/k3s.yaml": open '
    "/etc/rancher/k3s/k3s.yaml: permission denied\n"
)
# kubectl's stderr with no kubeconfig at all, as on a runner that ships kubectl.
_NO_CONFIGURATION = (
    "error: no configuration has been provided, try setting KUBERNETES_MASTER environment "
    "variable\n"
)


def _seam_outcome(stub: Path, tmp_path: Path) -> ET.Element:
    """Run the seam test in a child pytest with `stub` first on PATH, and return its testcase."""
    report = tmp_path / "report.xml"
    env = {k: v for k, v in os.environ.items() if k != "KUBECONFIG"}
    run(
        [
            sys.executable,
            "-m",
            "pytest",
            _SEAM,
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
    assert len(cases) == 1, f"{_SEAM} matched {len(cases)} tests"
    return cases[0]


@pytest.mark.parametrize(
    "stderr",
    [_UNREADABLE_KUBECONFIG, _NO_CONFIGURATION],
    ids=["unreadable-kubeconfig", "no-configuration"],
)
def test_the_live_jsonpath_seam_skips_a_kubectl_with_no_config(
    stderr, tmp_path
) -> None:
    stub = fake_bin(
        tmp_path / "bin", kubectl=f"cat >&2 <<'STDERR'\n{stderr}STDERR\nexit 1\n"
    )
    case = _seam_outcome(stub, tmp_path)
    failure = case.find("failure")
    detail = (
        failure.get("message") if failure is not None else ET.tostring(case, "unicode")
    )
    assert case.find("skipped") is not None, (
        f"the seam test did not skip a kubectl that can load no config: {detail}"
    )
