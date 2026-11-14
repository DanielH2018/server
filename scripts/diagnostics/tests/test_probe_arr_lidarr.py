"""`probe.py arr lidarr …`: Lidarr's API is readable read-only, like the other *arr apps (#8452).

Lidarr listens on 8686 and speaks /api/v1; its key is SOPS `lidarr_api_key`. The run_arr
tests put fake `kubectl`, `sops` and `curl` executables first on PATH and patch no module.
The fake curl answers like Lidarr only at Lidarr's URL with Lidarr's key, so what reaches
stdout depends on the probe routing the request correctly.
"""

import json
import sys

import pytest

from diagnostics.probe_lib import arr
from diagnostics.probe_lib.cli_parser import _build_parser
from lib.proc_testing import fake_bin, path_with

LIDARR_IP = "10.43.0.86"
LIDARR_KEY = "lidarr-key-sentinel"
WEBHOOK_SECRET = "LIDARR-WEBHOOK-TOKEN"

QUALITY_PROFILES = [
    {
        "id": 1,
        "name": "Standard",
        "upgradeAllowed": True,
        "cutoff": 1005,
        "items": [
            {"quality": {"id": 4, "name": "MP3-320"}, "items": [], "allowed": True}
        ],
        "minFormatScore": 0,
        "cutoffFormatScore": 0,
        "formatItems": [],
    },
    {
        "id": 2,
        "name": "Lossless",
        "upgradeAllowed": True,
        "cutoff": 1007,
        "items": [{"quality": {"id": 6, "name": "FLAC"}, "items": [], "allowed": True}],
        "minFormatScore": 0,
        "cutoffFormatScore": 0,
        "formatItems": [],
    },
]

NOTIFICATIONS = [
    {
        "id": 1,
        "name": "Discord (health)",
        "implementation": "Discord",
        "fields": [
            {
                "name": "webHookUrl",
                "value": f"https://discord.com/api/webhooks/9/{WEBHOOK_SECRET}",
                "privacy": "normal",
            },
            {"name": "username", "value": "Lidarr", "privacy": "normal"},
        ],
    }
]

ROUTES = {"qualityprofile": QUALITY_PROFILES, "notification": NOTIFICATIONS}

FAKE_KUBECTL = """
args = sys.argv[1:]
if "nodes" in args:
    print(json.dumps({"items": [{"metadata": {"name": "daniel-box"}}]}))
elif "service" in args and "lidarr" in args:
    print(LIDARR_IP, end="")
else:
    sys.exit("no such service")
"""

FAKE_SOPS = """
if '["lidarr_api_key"]' not in sys.argv:
    sys.exit("fake sops holds only lidarr_api_key")
print(LIDARR_KEY)
"""

FAKE_CURL = """
url, config = sys.argv[-1], sys.stdin.read()
prefix = f"http://{LIDARR_IP}:8686/api/v1/"
if not url.startswith(prefix) or LIDARR_KEY not in config:
    print("<html>401 Unauthorized</html>")
elif url[len(prefix):] not in ROUTES:
    print("<html>404 Not Found</html>")
else:
    print(json.dumps(ROUTES[url[len(prefix):]]))
"""


@pytest.fixture
def fake_lidarr(tmp_path, monkeypatch):
    """Fake kubectl, sops and curl that together answer like the cluster's Lidarr."""
    prelude = (
        f"#!{sys.executable}\nimport json, sys\n"
        f"LIDARR_IP = {LIDARR_IP!r}\nLIDARR_KEY = {LIDARR_KEY!r}\nROUTES = {ROUTES!r}\n"
    )
    bin_dir = fake_bin(
        tmp_path / "bin",
        kubectl=prelude + FAKE_KUBECTL,
        sops=prelude + FAKE_SOPS,
        curl=prelude + FAKE_CURL,
    )
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n")
    monkeypatch.setenv("PATH", path_with(bin_dir))
    monkeypatch.setenv("KUBECONFIG", str(kubeconfig))


def _parse(argv):
    try:
        return _build_parser().parse_args(argv)
    except SystemExit as exc:
        pytest.fail(f"probe.py refused {argv}: exit {exc.code}")


def test_arr_url_lidarr_port_8686_api_v1():
    assert arr.ARR_PORTS.get("lidarr") == 8686
    assert (
        arr.arr_url("10.0.0.8", "lidarr", "qualityprofile")
        == "http://10.0.0.8:8686/api/v1/qualityprofile"
    )


def test_arr_url_lidarr_normalizes_its_v1_prefix():
    assert arr.ARR_PORTS.get("lidarr") == 8686
    for path in ("qualityprofile", "/api/v1/qualityprofile", "v1/qualityprofile"):
        assert arr.arr_url("h", "lidarr", path) == "http://h:8686/api/v1/qualityprofile"


def test_arr_parser_accepts_lidarr():
    ns = _parse(["arr", "lidarr", "qualityprofile", "--json"])
    assert ns.app == "lidarr"


@pytest.mark.usefixtures("fake_lidarr")
def test_lidarr_qualityprofile_json_prints_both_profiles(capsys):
    rc = arr.run_arr(_parse(["arr", "lidarr", "qualityprofile", "--json"]))
    out = capsys.readouterr().out
    assert rc == 0
    assert {p["name"] for p in json.loads(out)} == {"Standard", "Lossless"}


@pytest.mark.usefixtures("fake_lidarr")
@pytest.mark.parametrize("extra", [[], ["--json"]])
def test_lidarr_output_is_redacted(capsys, extra):
    rc = arr.run_arr(_parse(["arr", "lidarr", "notification", *extra]))
    out = capsys.readouterr().out
    assert rc == 0
    assert WEBHOOK_SECRET not in out
    assert arr.REDACTED in out
