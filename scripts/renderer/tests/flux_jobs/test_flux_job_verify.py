"""The shared verify legs: the control passes on any answer, an isolation leg only on a refusal.

Every case runs over a real localhost socket, because `flux_job_connect.refused` keys on how
`urlopen` wraps each error. A port that is bound and not listening stands in for a fenced one.

Run: uv run pytest scripts/renderer/tests/flux_jobs/test_flux_job_verify.py
"""

import http.server
import socket
import threading

import pytest

import flux_job_verify


class _Status(http.server.BaseHTTPRequestHandler):
    status = 200

    def do_GET(self):
        self.send_response(self.status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format, *args):
        pass


@pytest.fixture
def serve():
    """Starts a server answering every GET with a status, and shuts each down after the test."""
    servers = []

    def start(status: int) -> str:
        handler = type("Handler", (_Status,), {"status": status})
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}/"

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


@pytest.fixture
def closed_url():
    """A URL on a bound port nothing listens on, held until the test ends."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        yield f"http://127.0.0.1:{sock.getsockname()[1]}/"


def _isolated(url: str) -> str:
    return flux_job_verify.isolated(url, accepted="ACCEPTED", answered="ANSWERED")


def test_the_control_passes_on_an_error_status(serve):
    """A 404 still proves the pod reached the Service."""
    url = serve(404)
    assert flux_job_verify.control(url, "HINT") == f"control ok: {url} answered"


def test_a_refused_control_fails_with_its_hint(closed_url):
    with pytest.raises(flux_job_verify.Failed, match=r"^CONTROL FAILED: .* HINT$"):
        flux_job_verify.control(closed_url, "HINT", retry_seconds=0)


def test_a_refused_isolation_leg_passes(closed_url):
    assert _isolated(closed_url) == f"isolated: {closed_url} refused, as intended"


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (200, "POLICY NOT ENFORCED: {url} answered ANSWERED"),
        (401, "no refusal from {url}"),
    ],
    ids=["ok", "error-status"],
)
def test_any_answer_fails_the_isolation_leg(serve, status, message):
    """An error status is a connection the server accepted, so it fails the leg too."""
    url = serve(status)
    with pytest.raises(flux_job_verify.Failed) as failed:
        _isolated(url)
    assert str(failed.value).startswith(message.format(url=url))


def test_a_dns_failure_fails_the_isolation_leg():
    """A lookup failure is no refusal, so it proves nothing about the policy."""
    with pytest.raises(
        flux_job_verify.Failed, match=r"Either ACCEPTED, or the connect"
    ):
        _isolated("http://verify.invalid:4466/")


def _run(env):
    if env.get("FAIL"):
        raise flux_job_verify.Failed("a leg failed")
    return ["one", "two"]


@pytest.mark.parametrize(
    ("env", "code", "out", "err"),
    [
        ({}, 0, "app-verify: one\napp-verify: two\n", ""),
        ({"FAIL": "1"}, 1, "", "app-verify: a leg failed\n"),
    ],
    ids=["pass", "fail"],
)
def test_main_prefixes_each_line_and_maps_failed_to_exit_1(env, code, out, err, capsys):
    assert flux_job_verify.main("app-verify", "Doc.\n", _run, [], env) == code
    assert capsys.readouterr() == (out, err)
