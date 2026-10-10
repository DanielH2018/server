"""The two Longhorn seam tests skip, not fail, when `kubectl` exists but can load no config (#4221).

As the `claude` agent user on daniel-box, `kubectl` is on PATH but falls back to
`/etc/rancher/k3s/k3s.yaml`, which only root reads. That is "no cluster to ask", the same as a
missing binary, and must skip the same way. A stub `kubectl` that fails every call with the
recorded stderr stands in for that client, so the case is reproduced on any machine.
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


@pytest.fixture
def unconfigured_kubectl(tmp_path, monkeypatch):
    """A `kubectl` first on PATH that fails every call, `config view` included, for want of a
    readable kubeconfig."""
    stub = fake_bin(
        tmp_path / "bin",
        kubectl=f"cat >&2 <<'STDERR'\n{_UNREADABLE_KUBECONFIG}STDERR\nexit 1\n",
    )
    monkeypatch.setenv("PATH", path_with(stub))
    monkeypatch.delenv("KUBECONFIG", raising=False)


def test_the_snapshot_listing_seam_skips_an_unreadable_kubeconfig(
    unconfigured_kubectl,
) -> None:
    from test_volume_snapshot import test_the_listing_jsonpath_parses

    with pytest.raises(pytest.skip.Exception):
        test_the_listing_jsonpath_parses()


def test_the_revert_listing_seam_skips_an_unreadable_kubeconfig(
    unconfigured_kubectl,
) -> None:
    from test_volume_revert import test_the_listing_jsonpath_parses

    with pytest.raises(pytest.skip.Exception):
        test_the_listing_jsonpath_parses()
