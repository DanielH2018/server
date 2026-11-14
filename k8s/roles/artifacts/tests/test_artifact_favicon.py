"""Tests for the favicon route, which every artifact page without its own icon falls back to."""

import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

import pytest

import artifact_server as srv


@pytest.fixture
def client(tmp_path):
    handler = type(
        "FaviconHandler", (srv.Handler,), {"cache": srv.IndexCache(tmp_path)}
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    ).start()
    conn = HTTPConnection("127.0.0.1", server.server_address[1])
    yield conn
    conn.close()
    server.shutdown()
    server.server_close()


def get(client, path):
    client.request("GET", path)
    res = client.getresponse()
    return res.status, res.getheader("Content-Type"), res.read()


def test_favicon_ico_serves_the_svg(client):
    assert get(client, "/favicon.ico") == (
        200,
        "image/svg+xml",
        srv.FAVICON_SVG.encode(),
    )


def test_the_gui_links_the_favicon(client):
    assert (
        b'<link rel="icon" type="image/svg+xml" href="/favicon.ico">'
        in get(client, "/")[2]
    )
