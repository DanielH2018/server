"""The Job connection helper retries a refused connect and never an answer.

Every case runs over a real localhost socket rather than a fake `urlopen`, because what the
helper keys on is how `urlopen` wraps each error, and a fake that raised a bare
`ConnectionRefusedError` would stay green while the retry never fired. The clock and the
sleep are the test's own, so the 60-second budget costs nothing.

Run: uv run pytest ansible/roles/k8s/manifests/tests/test_flux_job_connect.py
"""

import errno
import http.client
import http.server
import socket
import threading
import urllib.error

import pytest

import flux_job_connect


class _Clock:
    """A clock that moves only when the helper sleeps, and counts the sleeps."""

    def __init__(self, on_sleep=None):
        self.now = 0.0
        self.sleeps = 0
        self._on_sleep = on_sleep

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps += 1
        self.now += seconds
        if self._on_sleep:
            self._on_sleep()


class _Ok(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, format, *args):
        pass


class _Status(_Ok):
    def do_GET(self):
        self.send_error(503)


def _closed_port() -> int:
    """A localhost port nothing listens on, so a connect to it is refused."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def serve():
    """Starts an HTTP server on a port, and shuts every one down after the test."""
    servers = []

    def start(handler, port: int = 0) -> int:
        server = http.server.HTTPServer(("127.0.0.1", port), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return server.server_address[1]

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


@pytest.fixture
def accept_and_close():
    """A listener that accepts each connection and closes it unanswered; counts the accepts."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    accepted = []

    def run():
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            accepted.append(conn)
            conn.recv(4096)
            conn.close()

    threading.Thread(target=run, daemon=True).start()
    yield listener.getsockname()[1], accepted
    listener.close()


def test_a_refused_connect_is_retried_until_the_server_listens(serve):
    port = _closed_port()
    clock = _Clock(on_sleep=lambda: clock.sleeps == 2 and serve(_Ok, port))
    with flux_job_connect.open_url(
        f"http://127.0.0.1:{port}/", timeout=5, sleep=clock.sleep, clock=clock
    ) as response:
        assert response.read() == b"ok"
    assert clock.sleeps == 2


def test_a_refused_connect_raises_once_the_budget_is_spent():
    clock = _Clock()
    with pytest.raises(urllib.error.URLError) as raised:
        flux_job_connect.open_url(
            f"http://127.0.0.1:{_closed_port()}/",
            timeout=5,
            sleep=clock.sleep,
            clock=clock,
        )
    assert isinstance(raised.value.reason, ConnectionRefusedError)
    assert (
        clock.sleeps == flux_job_connect.RETRY_SECONDS // flux_job_connect.PAUSE_SECONDS
    )


def test_an_empty_answer_is_raised_without_a_retry(accept_and_close):
    port, accepted = accept_and_close
    clock = _Clock()
    with pytest.raises(http.client.RemoteDisconnected):
        flux_job_connect.open_url(
            f"http://127.0.0.1:{port}/", timeout=5, sleep=clock.sleep, clock=clock
        )
    assert (clock.sleeps, len(accepted)) == (0, 1)


def test_an_error_status_is_raised_without_a_retry(serve):
    port = serve(_Status)
    clock = _Clock()
    with pytest.raises(urllib.error.HTTPError) as raised:
        flux_job_connect.open_url(
            f"http://127.0.0.1:{port}/", timeout=5, sleep=clock.sleep, clock=clock
        )
    raised.value.close()
    assert (raised.value.code, clock.sleeps) == (503, 0)


def test_a_connect_timeout_is_a_refusal_and_a_read_timeout_is_an_answer():
    assert flux_job_connect.refused(urllib.error.URLError(TimeoutError()))
    assert not flux_job_connect.refused(TimeoutError())
    assert not flux_job_connect.refused(urllib.error.URLError(ConnectionResetError()))


def test_an_unreachable_host_is_a_refusal():
    unreachable = OSError(errno.EHOSTUNREACH, "No route to host")
    assert flux_job_connect.refused(urllib.error.URLError(unreachable))
