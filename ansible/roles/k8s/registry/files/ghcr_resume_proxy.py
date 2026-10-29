"""The ghcr-mirror's upstream: a loopback proxy to ghcr.io that resumes a cut blob with Range.

distribution's pull-through proxy fetches a blob from byte 0 and never resumes (#5654).
ghcr.io answers a blob GET with a 307 to a signed URL whose `se` expiry is minutes away, and
the signed host cuts the transfer at that expiry. While GHCR serves at 0.15-0.45 MB/s, a cold
blob over about 200 MB therefore never completes through the mirror, and containerd does not
fall back to ghcr.io on a short body (#6401).

`REGISTRY_PROXY_REMOTEURL` names this process instead of ghcr.io. Every request except a blob
GET passes through unchanged, the 401 challenge included, so distribution still fetches its
tokens from ghcr.io's realm. A blob GET is answered here: the proxy follows the 307 itself,
streams the body, and on a short read asks ghcr.io for a fresh signed URL and continues with
`Range: bytes=<sent>-`. A resumed answer that is not a 206 starting at that offset aborts the
response, so distribution sees a short body and stores nothing, rather than a spliced one.

Standard library only, because it runs in the stock python image from a ConfigMap.
"""

import argparse
import http.client
import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast
from urllib.parse import urlencode, urljoin, urlsplit

# `/v2/<repo>/blobs/sha256:<hex>`, where `<repo>` may hold slashes.
BLOB = re.compile(r"^/v2/(?P<repo>.+)/blobs/(?P<digest>sha256:[0-9a-f]{64})$")
OPEN_RANGE = re.compile(r"^bytes=(\d+)-$")
CONTENT_RANGE = re.compile(r"^bytes (\d+)-(\d+)/(\d+)$")
CHALLENGE_PARAM = re.compile(r'(\w+)="([^"]*)"')
FORWARDED = ("Authorization", "Accept", "User-Agent")
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "transfer-encoding",
    "te",
    "trailer",
    "upgrade",
}
CHUNK = 1 << 20


class Aborted(Exception):
    """A blob response cannot be completed, so its connection is closed short."""


def _connect(url: str, timeout: float) -> tuple[http.client.HTTPConnection, str]:
    parts = urlsplit(url)
    cls = (
        http.client.HTTPSConnection
        if parts.scheme == "https"
        else http.client.HTTPConnection
    )
    target = parts.path + (f"?{parts.query}" if parts.query else "")
    return cls(parts.netloc, timeout=timeout), target or "/"


def request(
    url: str, headers: dict[str, str], timeout: float, method: str = "GET"
) -> http.client.HTTPResponse:
    """Send one request without following a redirect; the caller closes the response."""
    conn, target = _connect(url, timeout)
    # One connection per request. The socket is handed to the response, as http.client does
    # for a response that closes its connection, so closing the response closes the socket.
    conn.request(method, target, headers={**headers, "Connection": "close"})
    resp = conn.getresponse()
    if conn.sock is not None:
        conn.sock.close()
        conn.sock = None
    return resp


def fetch_token(challenge: str, timeout: float) -> str | None:
    """Return an anonymous bearer token for a `WWW-Authenticate: Bearer ...` challenge.

    Returns None when the challenge is not a Bearer challenge or the realm refuses.
    """
    if not challenge.lower().startswith("bearer "):
        return None
    params = dict(CHALLENGE_PARAM.findall(challenge))
    realm = params.pop("realm", None)
    if not realm:
        return None
    resp = request(f"{realm}?{urlencode(params)}", {}, timeout)
    try:
        if resp.status != 200:
            return None
        body = json.loads(resp.read())
    finally:
        resp.close()
    return body.get("token") or body.get("access_token")


class Upstream:
    """One blob's upstream, which reopens it at any offset.

    Attributes:
        base: The upstream registry's origin, such as `https://ghcr.io`.
        path: The blob's `/v2/.../blobs/<digest>` path.
        headers: The headers sent to the registry; a refreshed token replaces Authorization.
        timeout: The socket timeout, which also bounds a stalled read.
    """

    def __init__(self, base: str, path: str, headers: dict[str, str], timeout: float):
        self.base = base
        self.path = path
        self.headers = headers
        self.timeout = timeout

    def open(self, offset: int, refresh: bool) -> http.client.HTTPResponse:
        """Return the blob's response from `offset`, following ghcr.io's redirect.

        Args:
            offset: The first byte wanted. Above zero, the signed URL is sent a Range.
            refresh: On a 401, fetch a fresh token from the challenge and retry once.
                Off for a client's first request, whose 401 belongs to distribution.
        """
        resp = request(self.base + self.path, self.headers, self.timeout)
        if resp.status == 401 and refresh:
            challenge = resp.getheader("WWW-Authenticate", "")
            resp.close()
            token = fetch_token(challenge, self.timeout)
            if token:
                self.headers = {**self.headers, "Authorization": f"Bearer {token}"}
            resp = request(self.base + self.path, self.headers, self.timeout)
        if resp.status not in (301, 302, 303, 307, 308):
            return resp
        location = urljoin(self.base + self.path, resp.getheader("Location", ""))
        resp.close()
        # The signed URL carries its own credential, and a second one is refused by some
        # blob stores, so the redirect hop sends no Authorization.
        hop = {k: v for k, v in self.headers.items() if k != "Authorization"}
        if offset:
            hop["Range"] = f"bytes={offset}-"
        return request(location, hop, self.timeout)


def resumed_at(resp: http.client.HTTPResponse, offset: int) -> int | None:
    """Return the blob's total size when `resp` continues it at `offset`, else None."""
    match = CONTENT_RANGE.match(resp.getheader("Content-Range", ""))
    if resp.status != 206 or not match or int(match.group(1)) != offset:
        return None
    return int(match.group(3))


class Handler(BaseHTTPRequestHandler):
    """Relays requests to `server.upstream`, answering blob GETs with a resuming stream."""

    protocol_version = "HTTP/1.1"

    @property
    def proxy(self) -> "ResumeProxy":
        return cast("ResumeProxy", self.server)

    def log_message(self, format: str, *args) -> None:
        sys.stderr.write("ghcr-resume: " + (format % args) + "\n")

    def _headers(self) -> dict[str, str]:
        return {k: v for k in FORWARDED if (v := self.headers.get(k))}

    def do_HEAD(self) -> None:
        self._relay("HEAD")

    def do_GET(self) -> None:
        match = BLOB.match(self.path)
        wanted = self.headers.get("Range")
        start = OPEN_RANGE.match(wanted) if wanted else None
        if not match or (wanted and not start):
            self._relay("GET")
            return
        try:
            self._blob(
                match.group("digest"), int(start.group(1)) if start else 0, bool(wanted)
            )
        except Aborted as err:
            self.log_message("aborted %s: %s", self.path, err)
            self.close_connection = True

    def _relay(self, method: str) -> None:
        headers = self._headers()
        if wanted := self.headers.get("Range"):
            headers["Range"] = wanted
        try:
            resp = request(
                self.proxy.upstream + self.path,
                headers,
                self.proxy.read_timeout,
                method,
            )
        except OSError as err:
            self.send_error(502, f"upstream unreachable: {err}")
            return
        with resp:
            self._copy(resp, method)

    def _copy(self, resp: http.client.HTTPResponse, method: str = "GET") -> None:
        body = b"" if method == "HEAD" else resp.read()
        self.send_response(resp.status, resp.reason)
        for key, value in resp.getheaders():
            if key.lower() not in HOP_BY_HOP and key.lower() != "content-length":
                self.send_header(key, value)
        length = (
            resp.getheader("Content-Length") if method == "HEAD" else str(len(body))
        )
        if length is not None:
            self.send_header("Content-Length", length)
        self.end_headers()
        self.wfile.write(body)

    def _blob(self, digest: str, start: int, ranged: bool) -> None:
        upstream = Upstream(
            self.proxy.upstream, self.path, self._headers(), self.proxy.read_timeout
        )
        try:
            resp = upstream.open(start, refresh=False)
        except OSError as err:
            self.send_error(502, f"upstream unreachable: {err}")
            return
        total = resumed_at(resp, start) if start else None
        if total is None and not start and resp.status == 200:
            total = int(resp.getheader("Content-Length", "-1"))
        if total is None or total < 0:
            # A 401, a 404 or an answer this proxy cannot resume: distribution reads it as is.
            with resp:
                self._copy(resp)
            return
        self.send_response(206 if ranged else 200)
        self.send_header(
            "Content-Type", resp.getheader("Content-Type", "application/octet-stream")
        )
        self.send_header("Content-Length", str(total - start))
        self.send_header("Docker-Content-Digest", digest)
        if ranged:
            self.send_header("Content-Range", f"bytes {start}-{total - 1}/{total}")
        self.end_headers()
        self._stream(upstream, resp, start, total)

    def _stream(
        self,
        upstream: Upstream,
        resp: http.client.HTTPResponse,
        offset: int,
        total: int,
    ) -> None:
        resumes = 0
        while offset < total:
            try:
                chunk = resp.read(min(CHUNK, total - offset))
            except (http.client.HTTPException, OSError) as err:
                chunk, cause = b"", err
            else:
                cause = "end of body"
            if chunk:
                self.wfile.write(chunk)
                offset += len(chunk)
                continue
            resp.close()
            resumes += 1
            if resumes > self.proxy.max_resumes:
                raise Aborted(
                    f"{resumes - 1} resumes and still short at byte {offset}/{total}"
                )
            self.log_message("cut at byte %d/%d (%s), resuming", offset, total, cause)
            try:
                resp = upstream.open(offset, refresh=True)
            except OSError as err:
                raise Aborted(f"resume at byte {offset} failed: {err}") from err
            if resumed_at(resp, offset) != total:
                status = resp.status
                resp.close()
                raise Aborted(
                    f"resume at byte {offset} answered {status} without its range"
                )
        resp.close()
        if resumes:
            self.log_message("completed %s after %d resume(s)", self.path, resumes)


class ResumeProxy(ThreadingHTTPServer):
    """The listening proxy.

    Attributes:
        upstream: The registry origin every request is relayed to.
        read_timeout: The socket timeout for each upstream connection, in seconds.
        max_resumes: How many cuts one blob response survives before it is aborted.
    """

    daemon_threads = True

    def __init__(
        self, address: tuple[str, int], upstream: str, timeout: float, max_resumes: int
    ):
        super().__init__(address, Handler)
        self.upstream = upstream.rstrip("/")
        self.read_timeout = timeout
        self.max_resumes = max_resumes


def main() -> None:
    argparse.ArgumentParser(
        description="Relay a registry to ghcr.io, resuming a cut blob GET with Range.",
        epilog="Configured by environment: LISTEN_PORT (required), LISTEN_HOST (127.0.0.1), "
        "UPSTREAM (https://ghcr.io), READ_TIMEOUT_S (60) and MAX_RESUMES (100).",
    ).parse_args()
    proxy = ResumeProxy(
        (os.environ.get("LISTEN_HOST", "127.0.0.1"), int(os.environ["LISTEN_PORT"])),
        os.environ.get("UPSTREAM", "https://ghcr.io"),
        float(os.environ.get("READ_TIMEOUT_S", "60")),
        int(os.environ.get("MAX_RESUMES", "100")),
    )
    proxy.serve_forever()


if __name__ == "__main__":
    main()
