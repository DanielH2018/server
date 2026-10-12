#!/usr/bin/env python3
"""Serve ~/.claude/artifacts from every homelab host at one authenticated URL.

Claude Code writes plan/spec/review artifacts to `~/.claude/artifacts` on each host
(artifact-design skill). Locally they are reachable over a loopback python -m http.server,
which only works from a terminal on that host. This serves the same trees behind Traefik +
Authelia, with a browsable index and search across every host's artifacts.

Layout: each host's tree is mounted read-only at ROOT/<host>, so a URL carries the host
that wrote the file:

    /                       the GUI
    /api/index.json         the index every search reads
    /api/star               POST: star or unstar one artifact (see StarStore)
    /a/<host>/<relpath>     one artifact, served from ROOT/<host>/<relpath>
    /healthz                liveness

The star list is the one thing the server writes. Every artifact tree stays mounted read-only;
the list lives in its own file, ARTIFACTS_STARS_FILE, which prune-artifacts.sh on each host
reads so that a starred artifact is never aged out.

The index is built ON REQUEST and cached against a signature of (path, mtime, size) for
every file under ROOT. It is not built on a timer: prune-artifacts.sh deletes artifacts
after 30 days (CLAUDE_ARTIFACT_RETENTION_DAYS in the dotfiles settings.base.json), so a
timer-built index serves links to files that are already gone. Building on request cannot. ~50 files parse in single-digit milliseconds.

Two siblings ship beside this one in the same ConfigMap: `artifact_meta.py` holds the
taxonomy and the metadata parsers, and `_gui_html.py` holds the page served at `/`.

Usage:

    python3 /app/artifact_server.py

Takes no arguments. The deployment runs it as the container command. It listens on all
interfaces and serves until killed. `-h` and `--help` print this text and exit 0.

Environment:

    ARTIFACTS_ROOT          directory holding one subdirectory per host (default /srv/artifacts)
    ARTIFACTS_PORT          TCP port to listen on (default 8080)
    ARTIFACTS_SERVICES_FILE JSON file of known service names, read by artifact_meta.py
    ARTIFACTS_STARS_FILE    the star list; unset turns starring off (default unset)
"""

from __future__ import annotations

import contextlib
import json
import os
import posixpath
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

# Flat siblings in the same directory, mounted together at /app from one ConfigMap, so a
# plain import resolves both under `python3 /app/artifact_server.py` and under the tests'
# path insert. Neither module imports back.
from _gui_html import GUI_HTML
from artifact_meta import (
    apply_metadata,
    load_known_services,
    parse_html,
    parse_markdown,
    slug_title,
)

ROOT = Path(os.environ.get("ARTIFACTS_ROOT", "/srv/artifacts"))
PORT = int(os.environ.get("ARTIFACTS_PORT", "8080"))
STARS_FILE = os.environ.get("ARTIFACTS_STARS_FILE", "")
# A star request carries one host and one relpath, so anything larger is not one.
MAX_STAR_BODY = 4096

INDEXABLE = {
    ".html",
    ".htm",
    ".md",
    ".txt",
    ".svg",
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
}
# Only these are parsed for metadata; the rest are listed with filesystem facts alone.
PARSEABLE = {".html", ".htm", ".md"}

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".md": "text/plain; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".svg": "image/svg+xml",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".json": "application/json",
}


def scan(root: Path) -> list[tuple[str, Path, os.stat_result]]:
    """Every indexable file under root, as (host, path, stat), sorted for a stable signature."""
    found: list[tuple[str, Path, os.stat_result]] = []
    if not root.is_dir():
        return found
    for host_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        for path in sorted(host_dir.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in INDEXABLE:
                continue
            try:
                found.append((host_dir.name, path, path.stat()))
            except OSError:
                # A file pruned between the walk and the stat is simply not indexed.
                continue
    return found


class StarStore:
    """The starred artifacts, one `<host>/<relpath>` line each, in a single file.

    `<host>` is the ROOT/<host> segment, which is also the label each host's
    prune-artifacts.sh carries (CLAUDE_ARTIFACTS_HOST). The prune keeps every artifact whose
    line names its own label, so one file serves every host it reaches.

    Attributes:
        path: The star file. A missing file means nothing is starred.
        writable: Whether POST /api/star may change the file.
    """

    def __init__(self, path: Path, writable: bool = True) -> None:
        self.path = path
        self.writable = writable
        # ThreadingHTTPServer handles requests concurrently, and two stars landing in the same
        # read-modify-write would lose one of them.
        self._lock = threading.Lock()

    def signature(self) -> tuple[int, int] | None:
        """The file's (mtime_ns, size), or None when it does not exist."""
        try:
            st = self.path.stat()
        except OSError:
            return None
        return st.st_mtime_ns, st.st_size

    def load(self, strict: bool = False) -> set[str]:
        """Reads the starred keys.

        Args:
            strict: Raise on a read error other than a missing file. `mark` reads strictly,
                because writing back an empty list over an unreadable one unstars everything.

        Returns:
            The set of `<host>/<relpath>` keys; empty when the file does not exist.

        Raises:
            OSError: `strict` is set and the file exists but cannot be read.
        """
        try:
            text = self.path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return set()
        except OSError:
            if strict:
                raise
            return set()
        return {line for line in text.splitlines() if line.strip()}

    def mark(self, key: str, starred: bool) -> None:
        """Adds or removes one key and replaces the file atomically.

        The new list is written beside the old one and renamed over it, so a prune reading
        the file mid-write sees one whole list or the other. A torn read would drop stars for
        that sweep, and dropping a star is how a starred artifact gets deleted.

        Raises:
            OSError: The file cannot be read, or its directory cannot be written.
        """
        with self._lock:
            keys = self.load(strict=True)
            if starred:
                keys.add(key)
            else:
                keys.discard(key)
            fd, tmp = tempfile.mkstemp(prefix=".stars.", dir=self.path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write("".join(f"{k}\n" for k in sorted(keys)))
                # mkstemp creates 0600. The agent user's prune on daniel-box reads this file
                # as another uid, and it lists file names, not secrets.
                os.chmod(tmp, 0o644)
                os.replace(tmp, self.path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
                raise


def build_index(
    root: Path,
    known_services: list[str] | None = None,
    stars: StarStore | None = None,
) -> dict:
    """Scans `root` and builds the searchable index of every artifact under it.

    Parses each HTML/Markdown file for title, summary, and metadata (matched against
    `known_services`, loaded from disk when not given), and flags a Markdown file with an
    HTML companion of the same stem so the GUI can fold the duplicate away.

    Args:
        root: Directory containing one subdirectory per host.
        known_services: Service names to match against metadata; loaded via
            load_known_services() when None.
        stars: The star list each entry's `starred` flag reads; None stars nothing and
            reports starring as unavailable.

    Returns:
        The index dict, with keys `generated`, `count`, `hosts`, `categories`, `statuses`,
        `star_writable` and `artifacts`.
    """
    known = load_known_services() if known_services is None else known_services
    starred = stars.load() if stars else set()
    entries = []
    for host, path, st in scan(root):
        rel = path.relative_to(root / host).as_posix()
        suffix = path.suffix.lower()
        entry = {
            "host": host,
            "name": path.name,
            "path": rel,
            "url": f"/a/{host}/{rel}",
            "kind": suffix.lstrip("."),
            "size": st.st_size,
            "mtime": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
            "starred": f"{host}/{rel}" in starred,
        }
        if suffix in PARSEABLE:
            try:
                body = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                body = ""
            entry.update(parse_html(body) if suffix != ".md" else parse_markdown(body))
        entry.setdefault("title", slug_title(path.name))
        entry.setdefault("summary", "")
        if suffix in PARSEABLE:
            apply_metadata(entry, body, suffix == ".md", known)
        # An .md alongside an .html of the same stem is the same document — the skill writes
        # both. Flagged so the GUI can fold the Markdown copy away by default.
        entry["companion_html"] = (
            suffix == ".md" and (path.parent / (path.stem + ".html")).exists()
        )
        entries.append(entry)
    entries.sort(key=lambda e: e["mtime"], reverse=True)
    return {
        "generated": datetime.now(timezone.utc).isoformat(),
        "count": len(entries),
        "hosts": sorted({e["host"] for e in entries}),
        # Facet values come from what the corpus actually holds, not a fixed vocabulary, so a
        # category nothing uses never appears as a dead dropdown entry.
        "categories": sorted({e["category"] for e in entries if e.get("category")}),
        "statuses": sorted({e["status"] for e in entries if e.get("status")}),
        "star_writable": bool(stars and stars.writable),
        "artifacts": entries,
    }


class IndexCache:
    """Rebuilds only when a file under root, or the star list, was added, removed, or changed."""

    def __init__(self, root: Path, stars: StarStore | None = None) -> None:
        self.root = root
        self.stars = stars
        self._signature: tuple | None = None
        self._index: dict | None = None

    def signature(self) -> tuple:
        files = tuple(
            (host, str(path), st.st_mtime_ns, st.st_size)
            for host, path, st in scan(self.root)
        )
        # A star changes no artifact, so without the list's own stat a toggle would never
        # reach the cached index.
        return files, self.stars.signature() if self.stars else None

    def get(self) -> dict:
        sig = self.signature()
        if sig != self._signature or self._index is None:
            self._index = build_index(self.root, stars=self.stars)
            self._signature = sig
        return self._index


def safe_path(root: Path, host: str, rel: str) -> Path | None:
    """Resolve /a/<host>/<rel> under root, or None if it escapes or does not exist.

    Traversal is rejected by resolving both sides and comparing prefixes, so `..`, an
    absolute path, and a symlink pointing out of the tree are all refused.

    DECIDED: the check is `os.path.realpath` then `str.startswith`, not `Path.resolve` then
    a `parents` test. Both reject the same inputs; only the first is the shape CodeQL's
    `py/path-injection` query recognises as a sanitizer (its normalization step is
    `os.path.normpath|abspath|realpath`, its safe-access check is `startswith`, and it models
    neither `Path.resolve` nor `Path.parents`). The pathlib form carried five hand-dismissed
    alerts that came back on every refactor. Full reasoning: ADR-0016.
    """
    if not host or "/" in host or host in (".", ".."):
        return None
    base = os.path.realpath(os.path.join(root, host))
    try:
        resolved = os.path.realpath(os.path.join(base, rel))
    except OSError, ValueError:
        return None
    if not resolved.startswith(base + os.sep):
        return None
    target = Path(resolved)
    if not target.is_file():
        return None
    return target


def render_gui() -> bytes:
    return GUI_HTML.encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    """Serves the GUI, index JSON, health check and stored artifacts, and takes star toggles.

    Attributes:
        cache: The shared IndexCache instance serving /api/index.json.
        stars: The star list POST /api/star changes; None turns starring off.
    """

    server_version = "artifacts/1.0"
    cache: IndexCache  # assigned in main() before the server starts
    stars: StarStore | None = None

    # `format` shadows the builtin, and is the parameter name BaseHTTPRequestHandler.log_message
    # declares — renaming it makes this an incompatible override.
    def log_message(self, format: str, *args: object) -> None:
        # One line per request on stdout, so `kubectl logs` reads like every other workload
        # here rather than BaseHTTPRequestHandler's stderr format.
        print(f"{self.address_string()} {format % args}", flush=True)

    def _send(
        self, status: int, body: bytes, ctype: str, extra: dict | None = None
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # These artifacts are internal reviews and audits behind Authelia; keep them out of
        # search engines even if the route is ever widened.
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        """Routes the request path to the GUI, health check, index JSON, or a stored artifact."""
        path = unquote(urlparse(self.path).path)
        if path in ("/", "/index.html"):
            self._send(200, render_gui(), "text/html; charset=utf-8")
            return
        if path == "/healthz":
            self._send(200, b"ok\n", "text/plain; charset=utf-8")
            return
        if path == "/api/index.json":
            body = json.dumps(self.cache.get()).encode("utf-8")
            self._send(200, body, "application/json")
            return
        if path.startswith("/a/"):
            self._serve_artifact(path[len("/a/") :])
            return
        self._send(404, b"not found\n", "text/plain; charset=utf-8")

    def do_POST(self) -> None:
        """Stars or unstars one artifact from a `{"host", "path", "starred"}` JSON body.

        Answers 200 with the stored state, 403 when starring is off, 415 for a non-JSON
        body, 400 for a malformed one, and 404 when host/path names no artifact.
        """
        text = "text/plain; charset=utf-8"
        if unquote(urlparse(self.path).path) != "/api/star":
            self._send(404, b"not found\n", text)
            return
        stars = self.stars
        if stars is None or not stars.writable:
            self._send(403, b"starring is not enabled on this server\n", text)
            return
        # The JSON content type is the CSRF defence. Another origin can POST a form or
        # text/plain without asking, but a JSON body needs a CORS preflight, which this
        # server never answers, so the browser never sends the request.
        ctype = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if ctype != "application/json":
            self._send(415, b"send application/json\n", text)
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            length = 0
        if not 0 < length <= MAX_STAR_BODY:
            self._send(400, b"bad Content-Length\n", text)
            return
        try:
            payload = json.loads(self.rfile.read(length))
            host, rel, starred = payload["host"], payload["path"], payload["starred"]
        except ValueError, KeyError, TypeError:
            self._send(400, b"expected {host, path, starred}\n", text)
            return
        if not (
            isinstance(host, str) and isinstance(rel, str) and isinstance(starred, bool)
        ):
            self._send(400, b"expected {host, path, starred}\n", text)
            return
        rel = posixpath.normpath(rel).lstrip("/")
        # A newline would split one key into two lines of the star file.
        if "\n" in rel or "\r" in rel or safe_path(self.cache.root, host, rel) is None:
            self._send(404, b"no such artifact\n", text)
            return
        try:
            stars.mark(f"{host}/{rel}", starred)
        except OSError as err:
            self._send(500, f"could not write the star list: {err}\n".encode(), text)
            return
        body = json.dumps({"host": host, "path": rel, "starred": starred})
        self._send(200, body.encode("utf-8"), "application/json")

    def _serve_artifact(self, rest: str) -> None:
        host, _, rel = rest.partition("/")
        # posixpath.normpath collapses `.`/`..` before safe_path resolves; both run, because
        # normpath alone does not stop a symlink and resolve() alone does not stop `%2e%2e`.
        rel = posixpath.normpath(rel).lstrip("/")
        target = safe_path(ROOT, host, rel)
        if target is None:
            self._send(404, b"not found\n", "text/plain; charset=utf-8")
            return
        try:
            body = target.read_bytes()
        except OSError:
            self._send(404, b"not found\n", "text/plain; charset=utf-8")
            return
        ctype = CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream")
        st = target.stat()
        self._send(
            200,
            body,
            ctype,
            {"Last-Modified": self.date_time_string(int(st.st_mtime))},
        )


def main() -> None:
    Handler.stars = StarStore(Path(STARS_FILE)) if STARS_FILE else None
    Handler.cache = IndexCache(ROOT, Handler.stars)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(
        f"serving {ROOT} on :{PORT} at {time.strftime('%Y-%m-%d %H:%M:%S')}", flush=True
    )
    server.serve_forever()


if __name__ == "__main__":
    if any(arg in ("-h", "--help") for arg in sys.argv[1:]):
        print(__doc__.strip())
        sys.exit(0)
    main()
