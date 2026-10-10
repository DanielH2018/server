"""Behavioural tests for host_lib — the shared I/O shell for gitops_deploy.py / renovate_notify.py.

host_lib is importable (stdlib only, no module-level config read), so its invariants are tested here
directly. This is the behavioural home of the Discord User-Agent + 2xx-only contract that gitops's
un-importable discord() previously pinned via AST guards (test_gitops_deploy_alert_delivery.py).
"""

import json
import os
import urllib.error
import urllib.parse
from email.message import Message
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


def test_clamp_discord_leaves_a_message_that_fits_alone():
    assert host_lib.clamp_discord("hello") == "hello"


def test_discord_post_clamps_the_marked_message_and_keeps_the_truncation_marker():
    # The cap applies after the marker prefix, and the cut message must END in the marker:
    # a reader cannot otherwise tell a cut message from a complete one (#3351).
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["req"] = req
        return _Resp(204)

    with mock.patch("host_lib.urllib.request.urlopen", fake_urlopen):
        host_lib.discord_post("https://x", "x" * 5000, "ua", marker="renovate:")
    content = json.loads(captured["req"].data)["content"]
    assert len(content) == host_lib.DISCORD_MAX
    assert content.startswith("renovate: x")
    assert content.endswith(host_lib.DISCORD_TRUNCATED)


# --- kuma_push ---------------------------------------------------------------------------
# The Python twin of kuma-push-lib.sh. The retry rule is the point: Traefik answers 404 for
# the whole of an uptime-kuma rollout (#1010), so a push that gives up on the first 404 is lost.


class _Body:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return b'{"ok":true}'


def _http_error(code, ctype="text/plain"):
    headers = Message()
    headers["Content-Type"] = ctype
    return urllib.error.HTTPError("https://k/api/push/T", code, "x", headers, None)


def _opener(*outcomes):
    """An opener that raises or answers each outcome in turn, recording the URLs it saw."""
    seen = []
    queue = list(outcomes)

    def opener(url, timeout=None):
        seen.append(url)
        outcome = queue.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return _Body()

    return opener, seen


def test_kuma_push_retries_a_rollout_404_and_delivers():
    opener, seen = _opener(_http_error(404), "ok")
    sleeps, logs = [], []
    assert host_lib.kuma_push(
        "up",
        "fine",
        "kuma.example",
        "T",
        log=logs.append,
        opener=opener,
        sleep=sleeps.append,
    )
    assert len(seen) == 2
    assert sleeps == [host_lib.KUMA_PUSH_RETRY_DELAY_S]
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(seen[0]).query)
    assert seen[0].startswith("https://kuma.example/api/push/T?")
    assert query["status"] == ["up"] and query["msg"] == ["fine"]


def test_kuma_push_to_a_base_url_keeps_its_scheme_and_port():
    """A pod pushes to the in-cluster Service over plain http, not through Traefik (#3745)."""
    opener, seen = _opener("ok")
    assert host_lib.kuma_push(
        "up", "fine", "http://uptime-kuma.homelab:3001/", "T", opener=opener
    )
    assert seen[0].startswith("http://uptime-kuma.homelab:3001/api/push/T?")


def test_kuma_push_stops_on_a_rejected_token_without_retrying():
    opener, seen = _opener(_http_error(401, "application/json"))
    sleeps, logs = [], []
    ok = host_lib.kuma_push(
        "down", "bad", "k", "T", log=logs.append, opener=opener, sleep=sleeps.append
    )
    assert ok is False
    assert len(seen) == 1 and sleeps == []
    assert "http=401 by=kuma" in logs[-1]


def test_kuma_push_gives_up_after_three_transport_failures_and_never_raises():
    opener, seen = _opener(
        TimeoutError(), OSError("reset"), urllib.error.URLError("dns")
    )
    logs = []
    ok = host_lib.kuma_push(
        "up", "m", "k", "SECRET", log=logs.append, opener=opener, sleep=lambda _s: None
    )
    assert ok is False
    assert len(seen) == host_lib.KUMA_PUSH_ATTEMPTS
    assert logs[-1].startswith("push failed (error=URLError)")
    assert not any("SECRET" in line for line in logs), (
        "a log line leaked the push token"
    )


def test_kuma_push_without_a_token_sends_nothing():
    opener, seen = _opener()
    logs = []
    assert (
        host_lib.kuma_push("up", "m", "k", "", log=logs.append, opener=opener) is False
    )
    assert seen == [] and logs


def test_cap_kuma_msg_cuts_to_the_limit_with_a_count_marker():
    capped = host_lib.cap_kuma_msg("y" * 2000)
    assert len(capped) == host_lib.KUMA_PUSH_MSG_MAX
    dropped = 2000 - capped.index(" …")
    assert capped.endswith(" …(+%d chars)" % dropped)
    assert host_lib.cap_kuma_msg("short") == "short"


# --- github_token / github_get -----------------------------------------------------------
# One lookup for every host GitHub reader (#3362). The anonymous limit is 60/hour per source
# IP and shared by every caller on the host, so each rule is an accept/reject pair: a resolver
# that always returned a token and one that never did are indistinguishable from one side.


class _GhProc:
    def __init__(self, returncode, stdout):
        self.returncode = returncode
        self.stdout = stdout


def test_github_token_from_the_source_wins_without_running_gh():
    def never(*_a, **_k):
        raise AssertionError("gh must not run when the source carries a token")

    assert host_lib.github_token({"GH_TOKEN": "ghp_env"}, never) == "ghp_env"
    assert host_lib.github_token({"GITHUB_TOKEN": "ghp_cfg"}, never) == "ghp_cfg"


def test_github_token_falls_back_to_gh_auth_token():
    calls = []

    def run(cmd, **_k):
        calls.append(cmd)
        return _GhProc(0, "gho_cli\n")

    assert host_lib.github_token({}, run) == "gho_cli"
    assert calls == [["gh", "auth", "token"]]


def test_github_token_with_no_token_anywhere_is_anonymous():
    """A logged-out gh, a missing binary or a blank value must degrade to anonymous."""
    assert host_lib.github_token({}, lambda *_a, **_k: _GhProc(1, "")) is None
    assert host_lib.github_token({}, lambda *_a, **_k: _GhProc(0, "  \n")) is None

    def boom(*_a, **_k):
        raise FileNotFoundError("gh")

    assert host_lib.github_token({"GH_TOKEN": "   "}, boom) is None


def test_github_get_sends_the_token_and_parses_the_body():
    captured = {}

    class _Json(_Body):
        def read(self, *_a):
            return b'{"check_runs": []}'

    def opener(req, timeout=None):
        captured["req"], captured["timeout"] = req, timeout
        return _Json()

    body = host_lib.github_get("repos/o/n/pulls", "tok", user_agent="ua", opener=opener)
    assert body == {"check_runs": []}
    req = captured["req"]
    assert req.full_url == "https://api.github.com/repos/o/n/pulls"
    assert req.get_header("Authorization") == "Bearer tok"
    assert req.get_header("User-agent") == "ua"
    assert captured["timeout"] == host_lib.GITHUB_TIMEOUT_S


def test_github_get_without_a_token_sends_no_authorization_header():
    captured = {}

    class _Json(_Body):
        def read(self, *_a):
            return b"[]"

    def opener(req, timeout=None):
        captured["req"] = req
        return _Json()

    host_lib.github_get("repos/o/n/issues", None, user_agent="ua", opener=opener)
    assert captured["req"].get_header("Authorization") is None


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


# ── rfc3339_to_epoch: shared with longhorn_backup_health_logic.py and longhorn_reap_logic.py ──
#
# The parser is shared because separate copies diverge: a copy that does not strip fractional
# seconds returns None for a Longhorn `snapshotCreatedAt` that another copy parses.


def test_rfc3339_to_epoch_parses_a_plain_z_timestamp():
    assert host_lib.rfc3339_to_epoch("2026-01-01T00:00:00Z") == 1767225600.0


def test_rfc3339_to_epoch_accepts_fractional_seconds():
    # The divergence: only one of the two original copies handled this.
    assert host_lib.rfc3339_to_epoch("2026-01-01T00:00:00.123456Z") == 1767225600.0


def test_rfc3339_to_epoch_accepts_a_numeric_utc_offset():
    assert host_lib.rfc3339_to_epoch("2026-01-01T00:00:00+00:00") == 1767225600.0


def test_rfc3339_to_epoch_rejects_garbage():
    assert host_lib.rfc3339_to_epoch("not-a-timestamp") is None


def test_rfc3339_to_epoch_rejects_empty_string():
    assert host_lib.rfc3339_to_epoch("") is None


# ── journal_reader: a cron's `logger` line is only evidence once something reads it back ──────


def test_journal_reader_returns_the_windows_messages_one_per_line():
    reader = host_lib.journal_reader("/bin/echo", 26, 10)
    lines = reader("longhorn-trim")
    assert lines is not None
    # /bin/echo prints the argv back, which is enough to assert the tag and the window reach it.
    assert "-t longhorn-trim" in lines[0]
    assert "--since -26h" in lines[0]
    assert "--output=cat" in lines[0]


def test_journal_reader_returns_none_when_the_read_itself_failed():
    """None and [] are different answers: a broken journalctl must not read as a quiet cron."""
    assert host_lib.journal_reader("/bin/false", 26, 10)("longhorn-trim") is None
    assert host_lib.journal_reader("/nonexistent/journalctl", 26, 10)("x") is None


# ── file_mtime: "the file is gone" and "I could not look" are different answers ────────────


def test_file_mtime_reads_an_existing_files_install_time(tmp_path):
    path = tmp_path / "longhorn-trim"
    path.touch()
    os.utime(path, (1_700_000_000, 1_700_000_000))
    assert host_lib.file_mtime(str(path)) == (1_700_000_000, False)


def test_file_mtime_separates_a_missing_file_from_one_it_cannot_stat(tmp_path):
    """A missing cron entry is a fault; an unstattable one is a permissions problem."""
    assert host_lib.file_mtime(str(tmp_path / "no-such-cron")) == (None, False)
    # A path under a non-directory: the stat raises ENOTDIR, not FileNotFoundError.
    not_a_dir = tmp_path / "file"
    not_a_dir.write_text("")
    assert host_lib.file_mtime(str(not_a_dir / "child")) == (None, True)
