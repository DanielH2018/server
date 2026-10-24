"""The transport, the poll loop and the command line of `k3s_gates`.

The transport tests run `Api` against a real local HTTP server, so the timeout, the bearer
header and every error path are measured on a socket, not on a stub that returns what the
test wants. TLS is exercised only as far as the missing-CA refusal, which needs no server.
"""

import http.server
import io
import threading
import time
from typing import ClassVar

import pytest
from _k3s_gates_fixtures import NOW, healthy

import k3s_gates
from k3s_gates import Api, ReadError


class _Handler(http.server.BaseHTTPRequestHandler):
    seen_auth: ClassVar[list[str]] = []

    def do_GET(self):
        type(self).seen_auth.append(self.headers.get("Authorization", ""))
        if self.path == "/ok":
            body, code = b'{"items": [1]}', 200
        elif self.path == "/forbidden":
            body, code = b'{"kind": "Status"}', 403
        elif self.path == "/text":
            body, code = b"not json", 200
        elif self.path == "/array":
            body, code = b"[1]", 200
        elif self.path == "/slow":
            time.sleep(1.0)
            body, code = b"{}", 200
        else:
            body, code = b"{}", 404
        self.send_response(code)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


@pytest.fixture
def server():
    _Handler.seen_auth = []
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def token(tmp_path) -> str:
    path = tmp_path / "token"
    path.write_text("s3cret\n")
    return str(path)


def test_a_read_is_a_json_object_and_sends_the_bearer_token(server, token):
    assert Api(server, token, "/nope")("/ok") == {"items": [1]}
    assert _Handler.seen_auth == ["Bearer s3cret"]


@pytest.mark.parametrize("path", ["/forbidden", "/missing", "/text", "/array"])
def test_an_error_status_or_an_unusable_body_is_refused(path, server, token):
    with pytest.raises(ReadError):
        Api(server, token, "/nope")(path)


def test_a_missing_token_file_is_refused(server, tmp_path):
    with pytest.raises(ReadError, match="token file"):
        Api(server, str(tmp_path / "absent"), "/nope")("/ok")


def test_an_empty_token_file_is_refused(server, tmp_path):
    (tmp_path / "t").write_text("\n")
    with pytest.raises(ReadError, match="empty"):
        Api(server, str(tmp_path / "t"), "/nope")("/ok")


def test_a_missing_ca_file_is_refused_before_any_connection(token, tmp_path):
    with pytest.raises(ReadError, match="CA file"):
        Api("https://127.0.0.1:1", token, str(tmp_path / "no-ca"))("/ok")


def test_a_closed_port_is_refused(token):
    with pytest.raises(ReadError):
        Api("http://127.0.0.1:1", token, "/nope", timeout=2)("/ok")


def test_a_slow_server_is_cut_off_at_the_read_timeout(server, token):
    started = time.monotonic()
    with pytest.raises(ReadError):
        Api(server, token, "/nope", timeout=0.2)("/slow")
    assert time.monotonic() - started < 0.9


def test_a_passed_deadline_refuses_every_read(server, token):
    api = Api(server, token, "/nope", deadline=time.monotonic() - 1)
    with pytest.raises(ReadError, match="deadline"):
        api("/ok")


def test_a_near_deadline_shortens_the_read_timeout(server, token):
    api = Api(server, token, "/nope", timeout=10, deadline=time.monotonic() + 0.2)
    started = time.monotonic()
    with pytest.raises(ReadError):
        api("/slow")
    assert time.monotonic() - started < 0.9


# ── the poll loop ────────────────────────────────────────────────────────────────────────────


def _loop(rounds, **kwargs):
    """Run `wait_for_pass` over checks that refuse for the first `rounds` rounds."""
    calls = {"n": 0}

    def check():
        calls["n"] += 1
        return ["still down"] if calls["n"] <= rounds else []

    slept, out = [], io.StringIO()
    clock = iter(range(0, 10_000, 30))
    code = k3s_gates.wait_for_pass(
        [("thing", check)],
        [],
        poll=30,
        sleep=slept.append,
        clock=lambda: next(clock),
        out=out,
        **kwargs,
    )
    return code, slept, out.getvalue()


def test_the_loop_passes_once_the_refusal_clears():
    code, slept, out = _loop(2, once=False, max_wait=None)
    assert (code, slept) == (0, [30, 30])
    assert out.count("REFUSE thing: still down") == 2
    assert "PASS" in out


def test_the_loop_gives_up_at_max_wait_instead_of_polling_forever():
    code, slept, out = _loop(10**6, once=False, max_wait=100)
    assert code == 1
    assert 0 < len(slept) <= 4
    assert "REFUSED: 1 check(s) refused" in out


def test_a_single_pass_refuses_without_sleeping():
    code, slept, _ = _loop(1, once=True, max_wait=None)
    assert (code, slept) == (1, [])


# ── the command line ─────────────────────────────────────────────────────────────────────────


def test_main_runs_host_mode_against_a_server_and_refuses_when_a_path_is_missing(
    server, token, capsys
):
    """Every path the server lacks is a 404, so the fake cluster refuses, and the exit is 1."""
    code = k3s_gates.main(
        ["host", "--server", server, "--token-file", token, "--ca-file", "/nope"]
    )
    assert code == 1
    assert "REFUSE volumes:" in capsys.readouterr().out


def test_main_without_a_server_refuses(monkeypatch, capsys):
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    assert k3s_gates.main(["host"]) == 1
    assert "no API server" in capsys.readouterr().err


def test_the_in_pod_server_url_brackets_an_ipv6_host(monkeypatch):
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "fd00::1")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT", "443")
    assert k3s_gates._default_server() == "https://[fd00::1]:443"
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.43.0.1")
    assert k3s_gates._default_server() == "https://10.43.0.1:443"


def test_the_fixture_cluster_is_a_pass_for_host_mode():
    """The accepting end of the main() pair: the same checks pass on a healthy cluster."""
    from _k3s_gates_fixtures import fake_get

    checks = k3s_gates.checks_for(
        "host", fake_get(healthy()), "/", None, lambda: NOW, False, []
    )
    assert k3s_gates.run_round(checks) == []
