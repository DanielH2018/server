"""obs_api: the Prometheus/Loki client probe.py, postflight and homelab-mcp share.

The URL builders keep their tests in test_probe.py under their `core.` spelling. What lives here
is what only homelab-mcp's copy exercises: the trailing window, the urllib transport, and the
import that has to succeed with no `scripts/` on the path.
"""

import json
import sys
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from diagnostics.probe_lib import obs_api
from lib import proc_testing

NOW = 1_700_000_000.0


def test_trailing_window_ends_at_now_and_spans_the_request():
    start, end = obs_api.trailing_window_ns(24 * 3600, NOW)
    assert end == int(NOW * 1e9)
    assert end - start == 24 * 3600 * 10**9


def test_trailing_window_accepts_a_fractional_span():
    start, end = obs_api.trailing_window_ns(0.5 * 3600, NOW)
    assert end - start == 1800 * 10**9


@pytest.mark.parametrize("seconds", [0, -1])
def test_trailing_window_rejects_a_nonpositive_span(seconds):
    with pytest.raises(ValueError):
        obs_api.trailing_window_ns(seconds, NOW)


@pytest.fixture
def server():
    """A loopback HTTP server: `/ok` answers JSON, anything else a 500."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            ok = self.path.startswith("/ok")
            body = json.dumps({"path": self.path}).encode() if ok else b"boom"
            self.send_response(200 if ok else 500)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def test_get_json_parses_a_2xx_body(server):
    url = obs_api.prom_query_url(f"{server}/ok", "up == 0")
    assert obs_api.get_json(url, timeout=5) == {
        "path": "/ok/api/v1/query?query=up+%3D%3D+0"
    }


def test_get_json_raises_on_a_5xx(server):
    with pytest.raises(urllib.error.HTTPError) as caught:
        obs_api.get_json(f"{server}/fail", timeout=5)
    assert caught.value.code == 500
    caught.value.close()


def test_imports_with_only_its_own_directory_on_the_path():
    """homelab-mcp's image carries obs_api.py flat at /app, with no `scripts/` beside it.

    A first-party import added here would pass every host-side test, because pytest puts
    `scripts/` on the path, and crash the pod at import. `-I` drops the cwd, PYTHONPATH and
    user site, so the module's own directory is the only first-party entry, as in the image.
    """
    probe_lib = Path(obs_api.__file__).resolve().parent
    out = proc_testing.run(
        [
            sys.executable,
            "-I",
            "-c",
            f"import sys; sys.path.insert(0, {str(probe_lib)!r}); import obs_api",
        ]
    )
    assert out.returncode == 0, out.stderr
