"""The cronjob-gate seam test skips, not fails, when `kubectl` exists but can load no config (#4221).

As the `claude` agent user on daniel-box, `kubectl` is on PATH but falls back to
`/etc/rancher/k3s/k3s.yaml`, which only root reads. That is "no cluster to ask", the same as a
missing binary, and must skip the same way. A stub `kubectl` that fails every call with the
recorded stderr stands in for that client, so the case is reproduced on any machine.
`test_cronjob_gate_decision.py` sits at its module-length ceiling, so this lives beside it.
"""

import pytest
from lib.proc_testing import fake_bin, path_with

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


def _kubectl_failing_with(stderr: str, tmp_path, monkeypatch) -> None:
    stub = fake_bin(
        tmp_path / "bin", kubectl=f"cat >&2 <<'STDERR'\n{stderr}STDERR\nexit 1\n"
    )
    monkeypatch.setenv("PATH", path_with(stub))
    monkeypatch.delenv("KUBECONFIG", raising=False)


@pytest.mark.parametrize(
    "stderr",
    [_UNREADABLE_KUBECONFIG, _NO_CONFIGURATION],
    ids=["unreadable-kubeconfig", "no-configuration"],
)
def test_the_live_jsonpath_seam_skips_a_kubectl_with_no_config(
    stderr, tmp_path, monkeypatch
) -> None:
    from test_cronjob_gate_decision import test_the_jsonpath_parses_against_the_live_api

    _kubectl_failing_with(stderr, tmp_path, monkeypatch)
    with pytest.raises(pytest.skip.Exception):
        test_the_jsonpath_parses_against_the_live_api()
