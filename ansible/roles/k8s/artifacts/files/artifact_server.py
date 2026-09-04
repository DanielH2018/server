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
    /a/<host>/<relpath>     one artifact, served from ROOT/<host>/<relpath>
    /healthz                liveness

The index is built ON REQUEST and cached against a signature of (path, mtime, size) for
every file under ROOT. It is not built on a timer: prune-artifacts.sh deletes artifacts
after 7 days, so a timer-built index serves links to files that are already gone. Building
on request cannot. ~50 files parse in single-digit milliseconds.

Two siblings ship beside this one in the same ConfigMap: `artifact_meta.py` holds the
taxonomy and the metadata parsers, and `_gui_html.py` holds the page served at `/`.
"""

from __future__ import annotations

import json
import os
import posixpath
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


def build_index(root: Path, known_services: list[str] | None = None) -> dict:
    """Scans `root` and builds the searchable index of every artifact under it.

    Parses each HTML/Markdown file for title, summary, and metadata (matched against
    `known_services`, loaded from disk when not given), and flags a Markdown file with an
    HTML companion of the same stem so the GUI can fold the duplicate away.

    Args:
        root: Directory containing one subdirectory per host.
        known_services: Service names to match against metadata; loaded via
            load_known_services() when None.

    Returns:
        The index dict, with keys `generated`, `count`, `hosts`, `categories`, `statuses`
        and `artifacts`.
    """
    known = load_known_services() if known_services is None else known_services
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
        "artifacts": entries,
    }


class IndexCache:
    """Rebuilds only when a file under root was added, removed, or changed."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._signature: tuple | None = None
        self._index: dict | None = None

    def signature(self) -> tuple:
        return tuple(
            (host, str(path), st.st_mtime_ns, st.st_size)
            for host, path, st in scan(self.root)
        )

    def get(self) -> dict:
        sig = self.signature()
        if sig != self._signature or self._index is None:
            self._index = build_index(self.root)
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
    """Handles GET/HEAD requests for the GUI, index JSON, health check, and stored artifacts.

    Attributes:
        cache: The shared IndexCache instance serving /api/index.json.
    """

    server_version = "artifacts/1.0"
    cache: IndexCache = None  # type: ignore[assignment]

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
    Handler.cache = IndexCache(ROOT)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(
        f"serving {ROOT} on :{PORT} at {time.strftime('%Y-%m-%d %H:%M:%S')}", flush=True
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
