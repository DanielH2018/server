"""The ghcr-mirror's resuming upstream completes a blob that ghcr.io's signed URL cuts short.

A fake ghcr.io stands upstream. It answers a blob GET with a 307 to a signed path, and the
signed path sends at most `cut` bytes of the blob before closing, the way the signed host cut
a transfer at its URL's expiry in #5654. A client reads through the proxy as distribution does.

Run: uv run pytest ansible/roles/k8s/registry/tests/test_ghcr_resume_proxy.py
"""

import hashlib
import http.client
import itertools
import re
import threading
from collections.abc import Generator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast

import pytest

import ghcr_resume_proxy

BLOB_DATA = bytes(range(256)) * 4096  # 1 MiB
DIGEST = "sha256:" + hashlib.sha256(BLOB_DATA).hexdigest()
BLOB_PATH = f"/v2/flaresolverr/flaresolverr/blobs/{DIGEST}"
MANIFEST = b'{"schemaVersion":2}'


class FakeGhcr(ThreadingHTTPServer):
    """ghcr.io's token realm, blob redirect and signed blob host.

    Attributes:
        cut: The most bytes one signed URL sends before it closes the connection.
        honour_range: When False, a signed URL ignores Range and sends from byte 0.
        tokens: The bearer tokens ghcr.io accepts; a test removes one to expire it.
        single_use: When True, a token is spent by the blob GET it authorises, so a
            resume needs a fresh one.
        signed_ranges: The Range header each signed-URL request carried, or "".
    """

    def __init__(self):
        self.cut = len(BLOB_DATA)
        self.honour_range = True
        self.tokens = {"first"}
        self.single_use = False
        self.signed_ranges: list[str] = []
        self._issued = itertools.count()
        super().__init__(("127.0.0.1", 0), _GhcrHandler)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def issue(self) -> str:
        token = f"t{next(self._issued)}"
        self.tokens.add(token)
        return token


class _GhcrHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args) -> None:
        pass

    def _send(self, status: int, body: bytes = b"", **headers: str) -> None:
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key.replace("_", "-"), value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        ghcr = cast(FakeGhcr, self.server)
        if self.path.startswith("/token?"):
            self._send(200, b'{"token": "%s"}' % ghcr.issue().encode())
            return
        if self.path.startswith("/signed/"):
            self._signed(ghcr)
            return
        auth = self.headers.get("Authorization", "").removeprefix("Bearer ")
        if auth not in ghcr.tokens:
            realm = f'Bearer realm="{ghcr.url}/token",service="ghcr.io",scope="pull"'
            self._send(401, b'{"errors":[]}', WWW_Authenticate=realm)
        elif self.path == BLOB_PATH:
            if ghcr.single_use:
                ghcr.tokens.discard(auth)
            self._send(307, Location=f"/signed/{DIGEST}?se=soon")
        elif self.path.endswith("/manifests/latest"):
            self._send(200, MANIFEST, Docker_Content_Digest="sha256:abc")
        else:
            self._send(404, b"{}")

    def _signed(self, ghcr: FakeGhcr) -> None:
        wanted = self.headers.get("Range", "")
        ghcr.signed_ranges.append(wanted)
        match = re.match(r"bytes=(\d+)-$", wanted)
        start = int(match.group(1)) if match and ghcr.honour_range else 0
        self.send_response(206 if start else 200)
        self.send_header("Content-Length", str(len(BLOB_DATA) - start))
        if start:
            self.send_header(
                "Content-Range", f"bytes {start}-{len(BLOB_DATA) - 1}/{len(BLOB_DATA)}"
            )
        self.end_headers()
        self.wfile.write(BLOB_DATA[start : start + ghcr.cut])
        self.close_connection = True


@contextmanager
def _serving(server: ThreadingHTTPServer) -> Generator[None]:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def ghcr() -> Generator[FakeGhcr]:
    server = FakeGhcr()
    with _serving(server):
        yield server


@pytest.fixture
def proxy(ghcr: FakeGhcr) -> Generator[ghcr_resume_proxy.ResumeProxy]:
    server = ghcr_resume_proxy.ResumeProxy(
        ("127.0.0.1", 0), ghcr.url, timeout=5, max_resumes=50
    )
    with _serving(server):
        yield server


def _get(
    proxy: ghcr_resume_proxy.ResumeProxy, path: str, **headers: str
) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", proxy.server_address[1], timeout=10)
    conn.request("GET", path, headers={"Authorization": "Bearer first", **headers})
    resp = conn.getresponse()
    try:
        return resp.status, dict(resp.getheaders()), resp.read()
    finally:
        conn.close()


def test_a_blob_cut_every_128_kib_arrives_whole(ghcr, proxy):
    ghcr.cut = 128 * 1024

    status, headers, body = _get(proxy, BLOB_PATH)

    assert status == 200
    assert headers["Content-Length"] == str(len(BLOB_DATA))
    assert "sha256:" + hashlib.sha256(body).hexdigest() == DIGEST
    # One fresh signed URL per cut, each asked for the byte after the last one sent.
    assert ghcr.signed_ranges == [""] + [f"bytes={n * ghcr.cut}-" for n in range(1, 8)]


def test_a_resume_answered_from_byte_zero_aborts_rather_than_splices(ghcr, proxy):
    ghcr.cut = 300 * 1024
    ghcr.honour_range = False

    with pytest.raises(http.client.IncompleteRead) as cut:
        _get(proxy, BLOB_PATH)

    assert cut.value.partial == BLOB_DATA[: ghcr.cut]


def test_a_token_that_expires_mid_blob_is_replaced_from_the_challenge(ghcr, proxy):
    ghcr.cut = 512 * 1024
    ghcr.single_use = True

    _, _, body = _get(proxy, BLOB_PATH)

    assert body == BLOB_DATA
    assert "first" not in ghcr.tokens


def test_a_client_range_is_answered_from_its_offset(ghcr, proxy):
    ghcr.cut = 256 * 1024

    status, headers, body = _get(proxy, BLOB_PATH, Range="bytes=1000-")

    assert status == 206
    assert (
        headers["Content-Range"] == f"bytes 1000-{len(BLOB_DATA) - 1}/{len(BLOB_DATA)}"
    )
    assert body == BLOB_DATA[1000:]


def test_a_challenge_and_a_manifest_pass_through_unchanged(proxy):
    status, headers, _ = _get(proxy, "/v2/", Authorization="Bearer stale")
    assert status == 401
    assert headers["WWW-Authenticate"].startswith("Bearer realm=")

    status, headers, body = _get(
        proxy, "/v2/flaresolverr/flaresolverr/manifests/latest"
    )
    assert (status, headers["Docker-Content-Digest"], body) == (
        200,
        "sha256:abc",
        MANIFEST,
    )
