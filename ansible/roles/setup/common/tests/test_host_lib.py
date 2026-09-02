"""Behavioural tests for host_lib — the shared I/O shell for gitops_deploy.py / renovate_notify.py.

host_lib is importable (stdlib only, no module-level config read), so its invariants are tested here
directly. This is the behavioural home of the Discord User-Agent + 2xx-only contract that gitops's
un-importable discord() previously pinned via AST guards (test_gitops_deploy_alert_delivery.py).
"""

import json
from unittest import mock

import host_lib


def test_parse_env_file_skips_comments_and_splits_on_first_equals(tmp_path):
    p = tmp_path / "config.env"
    p.write_text("# a comment\nA=1\nB=x=y\n\n  \nNOEQUALS\n")
    assert host_lib.parse_env_file(str(p)) == {"A": "1", "B": "x=y"}


def test_atomic_write_creates_dirs_replaces_and_leaves_no_tmp(tmp_path):
    p = tmp_path / "sub" / "state"
    host_lib.atomic_write(str(p), "hello")
    assert p.read_text() == "hello"
    host_lib.atomic_write(str(p), "world")  # overwrite
    assert p.read_text() == "world"
    assert not (tmp_path / "sub" / "state.tmp").exists()


class _Resp:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_discord_post_empty_webhook_is_false_and_logs():
    logs = []
    assert host_lib.discord_post("", "hi", "ua", log=logs.append) is False
    assert logs  # a skip reason was logged


def test_discord_post_sends_user_agent_and_true_on_2xx():
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["req"] = req
        return _Resp(204)

    with mock.patch("host_lib.urllib.request.urlopen", fake_urlopen):
        ok = host_lib.discord_post("https://example/webhook", "hello", "gitops-deploy")
    assert ok is True
    req = captured["req"]
    # urllib capitalises header keys ("User-agent"); assert on the value to stay robust.
    assert "gitops-deploy" in req.headers.values()
    assert json.loads(req.data)["content"] == "hello"


def test_discord_post_prepends_marker():
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["req"] = req
        return _Resp(204)

    with mock.patch("host_lib.urllib.request.urlopen", fake_urlopen):
        host_lib.discord_post(
            "https://x", "3 fakes held", "ua", marker="📼 fake-remux:"
        )
    assert json.loads(captured["req"].data)["content"] == "📼 fake-remux: 3 fakes held"


def test_discord_post_no_marker_leaves_content_unchanged():
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["req"] = req
        return _Resp(204)

    with mock.patch("host_lib.urllib.request.urlopen", fake_urlopen):
        host_lib.discord_post("https://x", "plain", "ua")
    assert json.loads(captured["req"].data)["content"] == "plain"


def test_discord_post_false_on_non_2xx():
    with mock.patch(
        "host_lib.urllib.request.urlopen", lambda req, timeout=None: _Resp(500)
    ):
        assert host_lib.discord_post("https://x", "hi", "ua") is False


def test_discord_post_false_and_logs_on_exception():
    def boom(req, timeout=None):
        raise OSError("network down")

    logs = []
    with mock.patch("host_lib.urllib.request.urlopen", boom):
        ok = host_lib.discord_post("https://x", "hi", "ua", log=logs.append)
    assert ok is False
    assert logs  # the failure was logged, not raised


# --- kubectl_runner ------------------------------------------------------------------------
# The single source for what janitorr_health.py and configarr_health.py each carried a
# byte-identical copy of. Each rule is an accept/reject pair: what the runner must return on a
# clean call, and what it must return on each failure it is supposed to distinguish.


class _Proc:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def test_a_successful_call_returns_stdout():
    with mock.patch(
        "host_lib.subprocess.run", lambda *a, **k: _Proc(0, "pods", "noise")
    ):
        assert host_lib.kubectl_runner("k3s kubectl", "homelab", 30)("get", "pod") == (
            0,
            "pods",
        )


def test_a_failing_call_returns_stderr():
    """The caller reports the reason without branching, so the failing half must carry it."""
    with mock.patch(
        "host_lib.subprocess.run", lambda *a, **k: _Proc(1, "", "NotFound")
    ):
        assert host_lib.kubectl_runner("k3s kubectl", "homelab", 30)("get", "pod") == (
            1,
            "NotFound",
        )


def test_the_binary_namespace_and_args_reach_the_subprocess():
    seen = {}

    def capture(argv, **kwargs):
        seen["argv"] = argv
        return _Proc(0, "", "")

    with mock.patch("host_lib.subprocess.run", capture):
        host_lib.kubectl_runner("k3s kubectl", "homelab", 30)("get", "pod")
    assert seen["argv"] == ["k3s", "kubectl", "-n", "homelab", "get", "pod"]


def test_a_timeout_is_distinct_from_a_cluster_refusal():
    def boom(*a, **k):
        raise host_lib.subprocess.TimeoutExpired(cmd="kubectl", timeout=30)

    with mock.patch("host_lib.subprocess.run", boom):
        rc, msg = host_lib.kubectl_runner("k3s kubectl", "homelab", 30)("get", "pod")
    assert rc == host_lib.KUBECTL_TIMEOUT_RC
    assert "timed out" in msg


def test_an_unrunnable_binary_is_distinct_from_a_timeout():
    def boom(*a, **k):
        raise OSError("No such file or directory: 'k3s'")

    with mock.patch("host_lib.subprocess.run", boom):
        rc, msg = host_lib.kubectl_runner("k3s kubectl", "homelab", 30)("get", "pod")
    assert rc == host_lib.KUBECTL_UNRUNNABLE_RC
    assert rc != host_lib.KUBECTL_TIMEOUT_RC
    assert "could not run kubectl" in msg


def test_local_bin_is_prepended_when_the_path_omits_it(monkeypatch):
    """Cron's default PATH omits /usr/local/bin, where both k3s and kubectl live."""
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    seen = {}

    def capture(argv, **kwargs):
        seen["path"] = kwargs["env"]["PATH"]
        return _Proc(0, "", "")

    with mock.patch("host_lib.subprocess.run", capture):
        host_lib.kubectl_runner("k3s kubectl", "homelab", 30)("get", "pod")
    assert seen["path"].split(":")[0] == host_lib.LOCAL_BIN


def test_local_bin_is_not_duplicated_when_the_path_already_has_it(monkeypatch):
    monkeypatch.setenv("PATH", f"{host_lib.LOCAL_BIN}:/usr/bin")
    seen = {}

    def capture(argv, **kwargs):
        seen["path"] = kwargs["env"]["PATH"]
        return _Proc(0, "", "")

    with mock.patch("host_lib.subprocess.run", capture):
        host_lib.kubectl_runner("k3s kubectl", "homelab", 30)("get", "pod")
    assert seen["path"].count(host_lib.LOCAL_BIN) == 1


def test_the_rest_of_the_environment_survives(monkeypatch):
    """KUBECONFIG is the caller's job, so the runner must not drop it."""
    monkeypatch.setenv("KUBECONFIG", "/home/ubuntu/.kube/config")
    seen = {}

    def capture(argv, **kwargs):
        seen["env"] = kwargs["env"]
        return _Proc(0, "", "")

    with mock.patch("host_lib.subprocess.run", capture):
        host_lib.kubectl_runner("k3s kubectl", "homelab", 30)("get", "pod")
    assert seen["env"]["KUBECONFIG"] == "/home/ubuntu/.kube/config"
