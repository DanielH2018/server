import json
import subprocess

from lib import host_modules  # noqa: F401
from lib.repo_paths import GH_QUOTA_FILES
from gh_quota import Ledger

from deploy_tools import forge


def test_each_rest_request_is_counted_under_the_token_it_sends(monkeypatch, tmp_path):
    monkeypatch.setenv("GH_QUOTA_MODULE", str(GH_QUOTA_FILES / "gh_quota.py"))
    monkeypatch.setenv("GH_QUOTA_MODE", "record")
    monkeypatch.setenv("GH_QUOTA_DIR", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "promote-session")
    resources = {
        "graphql": {"used": 0, "limit": 5000, "reset": 0},
        "core": {"used": 40, "limit": 5000, "reset": 4_000_000_000},
    }

    def sample(argv, **_kwargs):
        return subprocess.CompletedProcess(
            argv, 0, json.dumps({"resources": resources}), ""
        )

    # Reading the ledger first takes the sample, so the requests below run none of their own.
    ledger = Ledger(root=tmp_path, env={"GH_TOKEN": "forge-token"}, run=sample)
    ledger.windows()

    def transport(method, url, headers):
        body = b"[]" if method == "GET" else b""
        return forge.Response(200 if method == "GET" else 204, body=body)

    github = forge.GitHub(transport, lambda: "forge-token", sleep=lambda _s: None)
    github.package_versions()
    github.delete_package_version(7)

    (window,) = ledger.windows()
    assert window.sessions == {
        "promote-session": {
            "api rest users/{u}/packages/container/homelab-manifests/versions": 1,
            "api write": 1,
        }
    }
