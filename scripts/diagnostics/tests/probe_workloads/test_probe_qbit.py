"""`probe.py qbit …`: qBittorrent's WebUI API is readable with an API key, never a login.

The run_qbit tests put fake `kubectl`, `sops`, `curl` and `ssh` executables first on PATH and
patch no module. The fake curl answers like qBittorrent only at its /api/v2 URL with the bearer
key on stdin, and only on the node holding the pod, as the NetworkPolicy refuses a host process
on the other node.
"""

import json
import socket
import sys

import pytest

from diagnostics.probe_lib import qbit
from diagnostics.probe_lib.cli_parser import _build_parser
from lib.proc_testing import fake_bin, path_with

QBIT_IP = "10.43.0.80"
QBIT_KEY = "qbt_keysentinel0000000000000000"
PROXY_PASSWORD = "PROXY-PASSWORD-SENTINEL"
HERE = socket.gethostname().split(".")[0]

ROUTES = {
    "torrents/info?category=lidarr": [
        {"name": "Album A", "category": "lidarr", "state": "stalledDL"},
        {"name": "Album B", "category": "lidarr", "state": "queuedDL"},
    ],
    "app/preferences": {
        "max_active_downloads": 5,
        "dont_count_slow_torrents": False,
        "proxy_password": PROXY_PASSWORD,
    },
}

FAKE_KUBECTL = """
args = sys.argv[1:]
if "nodes" in args:
    print(json.dumps({"items": [{"metadata": {"name": "daniel-box"}}]}))
elif "endpointslices" in args and "kubernetes.io/service-name=qbittorrent" in args:
    endpoint = {"nodeName": os.environ["QBIT_NODE"], "conditions": {"ready": True}}
    labels = {"kubernetes.io/service-name": "qbittorrent"}
    print(json.dumps({"items": [{"metadata": {"labels": labels}, "endpoints": [endpoint]}]}))
elif "service" in args and "qbittorrent" in args:
    print(QBIT_IP, end="")
else:
    sys.exit("no such service")
"""

FAKE_SOPS = """
if '["qbittorrent_api_key"]' not in sys.argv:
    sys.exit("fake sops holds only qbittorrent_api_key")
print(QBIT_KEY)
"""

FAKE_CURL = """
url, config = sys.argv[-1], sys.stdin.read()
if os.environ.get("FAKE_RUNS_ON", HERE) != os.environ["QBIT_NODE"]:
    sys.exit("curl: (7) Failed to connect")
prefix = f"http://{QBIT_IP}:8080/api/v2/"
if f"Authorization: Bearer {QBIT_KEY}" not in config:
    print("Forbidden")
elif not url.startswith(prefix) or url[len(prefix):] not in ROUTES:
    print("Not Found")
else:
    print(json.dumps(ROUTES[url[len(prefix):]]))
"""

FAKE_SSH = """
args = sys.argv[1:]
while args[0] == "-o":
    args = args[2:]
host, remote = args
env = {**os.environ, "FAKE_RUNS_ON": host}
sys.exit(subprocess.run(shlex.split(remote), env=env).returncode)
"""


@pytest.fixture
def fake_qbit(tmp_path, monkeypatch):
    """Fake kubectl, sops, curl and ssh that together answer like the cluster's qBittorrent."""
    prelude = (
        f"#!{sys.executable}\nimport json, os, shlex, subprocess, sys\n"
        f"QBIT_IP = {QBIT_IP!r}\nQBIT_KEY = {QBIT_KEY!r}\nROUTES = {ROUTES!r}\n"
        f"HERE = {HERE!r}\n"
    )
    bin_dir = fake_bin(
        tmp_path / "bin",
        kubectl=prelude + FAKE_KUBECTL,
        sops=prelude + FAKE_SOPS,
        curl=prelude + FAKE_CURL,
        ssh=prelude + FAKE_SSH,
    )
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n")
    monkeypatch.setenv("PATH", path_with(bin_dir))
    monkeypatch.setenv("KUBECONFIG", str(kubeconfig))
    monkeypatch.setenv("QBIT_NODE", HERE)
    monkeypatch.delenv("FAKE_RUNS_ON", raising=False)


def _run(argv):
    return qbit.run_qbit(_build_parser().parse_args(["qbit", *argv]))


@pytest.mark.parametrize(
    "path",
    [
        "app/preferences",
        "/app/preferences",
        "api/v2/app/preferences",
        "v2/app/preferences",
    ],
)
def test_every_path_spelling_reaches_api_v2(path):
    assert qbit.qbit_url("h", path) == "http://h:8080/api/v2/app/preferences"


@pytest.mark.usefixtures("fake_qbit")
def test_torrents_are_read_with_the_bearer_key(capsys):
    assert _run(["torrents/info?category=lidarr", "--json"]) == 0
    states = [t["state"] for t in json.loads(capsys.readouterr().out)]
    assert states == ["stalledDL", "queuedDL"]


@pytest.mark.usefixtures("fake_qbit")
def test_preference_passwords_are_redacted(capsys):
    assert _run(["app/preferences", "--json"]) == 0
    prefs = json.loads(capsys.readouterr().out)
    assert prefs["proxy_password"] == qbit.core.REDACTED
    assert prefs["max_active_downloads"] == 5


@pytest.mark.usefixtures("fake_qbit")
def test_a_pod_on_the_other_node_is_read_from_that_node(capsys, monkeypatch):
    monkeypatch.setenv("QBIT_NODE", f"not-{HERE}")
    assert _run(["app/preferences", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["max_active_downloads"] == 5
