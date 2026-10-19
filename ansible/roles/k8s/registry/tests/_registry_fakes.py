"""An in-memory registry speaking the part of the distribution API `registry_verify` uses.

One server plays the push store in `test_registry_verify.py`, and the source registry and the
push store in `test_registry_verify_seed.py`. As a source it can demand a bearer token the way
Docker Hub does, and redirect each blob GET to a second path the way Docker Hub's CDN does.
"""

import hashlib
import json
import re
import threading
from collections.abc import Generator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast
from urllib.parse import parse_qs, urlsplit

# `/v2/<repo>/<kind>/<ref>`, where `<repo>` may hold slashes, as `library/python` does.
OBJECT = re.compile(r"^/v2/(?P<repo>.+)/(?P<kind>manifests|blobs)/(?P<ref>[^/]+)$")
UPLOAD = re.compile(r"^/v2/(?P<repo>.+)/blobs/uploads/")
TOKEN = "fake-token"


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class FakeRegistry(ThreadingHTTPServer):
    """The registry's state, which a test reads and seeds directly.

    Attributes:
        blobs: The stored blobs, by digest.
        manifests: The stored manifests, by `<repo>:<tag or digest>`.
        media: Each stored manifest's media type, by the same key.
        uploads: How many blob uploads completed.
        corrupt: When set, a manifest GET serves these bytes instead of the stored ones.
        token_realm: When set, a GET answers 401 without `Bearer <TOKEN>` and names this
            realm, which this server also answers at `/token`.
        redirect_blobs: A blob GET answers 307 to `/cdn/<digest>`, which serves the blob.
        cdn_auth: The `Authorization` header each `/cdn/` request carried, or "".
    """

    def __init__(self):
        self.blobs: dict[str, bytes] = {}
        self.manifests: dict[str, bytes] = {}
        self.media: dict[str, str] = {}
        self.uploads = 0
        self.corrupt: bytes | None = None
        self.token_realm: str | None = None
        self.redirect_blobs = False
        self.cdn_auth: list[str] = []
        super().__init__(("127.0.0.1", 0), _Handler)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def hold(self, repo: str, ref: str, body: bytes, media: str) -> None:
        """Store manifest `body` as `repo:ref` and under its own digest."""
        for key in (f"{repo}:{ref}", f"{repo}:{digest(body)}"):
            self.manifests[key] = body
            self.media[key] = media


@contextmanager
def serving() -> Generator[FakeRegistry]:
    server = FakeRegistry()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


class _Handler(BaseHTTPRequestHandler):
    @property
    def registry(self) -> FakeRegistry:
        return cast(FakeRegistry, self.server)

    def _send(self, status, headers=None, body=b""):
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _body(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def _authorized(self) -> bool:
        realm = self.registry.token_realm
        if realm is None or self.headers.get("Authorization") == f"Bearer {TOKEN}":
            return True
        challenge = f'Bearer realm="{realm}",service="fake",scope="repository:x:pull"'
        self._send(401, {"WWW-Authenticate": challenge})
        return False

    def do_HEAD(self):
        match = OBJECT.match(urlsplit(self.path).path)
        if not match:
            self._send(404)
            return
        repo, kind, ref = match["repo"], match["kind"], match["ref"]
        found = (
            ref in self.registry.blobs
            if kind == "blobs"
            else f"{repo}:{ref}" in self.registry.manifests
        )
        self._send(200 if found else 404)

    def do_POST(self):
        self._body()
        match = UPLOAD.match(self.path)
        assert match, self.path
        self._send(202, {"Location": f"/v2/{match['repo']}/blobs/uploads/abc?state=x"})

    def do_PUT(self):
        url = urlsplit(self.path)
        body = self._body()
        if UPLOAD.match(url.path):
            want = parse_qs(url.query)["digest"][0]
            if digest(body) != want:
                self._send(400)
                return
            self.registry.blobs[want] = body
            self.registry.uploads += 1
            self._send(201)
            return
        match = OBJECT.match(url.path)
        assert match, url.path
        self.registry.hold(
            match["repo"], match["ref"], body, self.headers.get("Content-Type", "")
        )
        self._send(201)

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path == "/token":
            self._send(200, body=json.dumps({"token": TOKEN}).encode())
            return
        if url.path.startswith("/cdn/"):
            self.registry.cdn_auth.append(self.headers.get("Authorization", ""))
            self._send(200, body=self.registry.blobs[url.path.removeprefix("/cdn/")])
            return
        if not self._authorized():
            return
        match = OBJECT.match(url.path)
        assert match, url.path
        repo, kind, ref = match["repo"], match["kind"], match["ref"]
        if kind == "blobs":
            if self.registry.redirect_blobs:
                self._send(307, {"Location": f"/cdn/{ref}"})
            else:
                self._send(200, body=self.registry.blobs[ref])
            return
        key = f"{repo}:{ref}"
        body = self.registry.manifests.get(key)
        if body is None:
            self._send(404)
            return
        served = self.registry.corrupt or body
        headers = {"Docker-Content-Digest": digest(served)}
        if self.registry.media.get(key):
            headers["Content-Type"] = self.registry.media[key]
        self._send(200, headers, served)

    def log_message(self, format, *args):
        pass
