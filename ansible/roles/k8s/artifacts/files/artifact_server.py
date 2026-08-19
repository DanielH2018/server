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
"""

from __future__ import annotations

import html
import json
import os
import re
import posixpath
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(os.environ.get("ARTIFACTS_ROOT", "/srv/artifacts"))
PORT = int(os.environ.get("ARTIFACTS_PORT", "8080"))
# How much stripped body text goes into the index, so search reaches content and not just
# titles. 2 KB per artifact keeps a ~50-file index well under a megabyte.
EXCERPT_CHARS = int(os.environ.get("ARTIFACTS_EXCERPT_CHARS", "2000"))

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

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.I | re.S)
_UPDATED_RE = re.compile(r"""data-updated=["']([^"']+)["']""", re.I)
_SLICE_RE = re.compile(r"""data-status=["'](planned|active|done)["']""", re.I)
_META_DESC_RE = re.compile(
    r"""<meta[^>]+name=["']description["'][^>]+content=["']([^"']*)["']""", re.I
)
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.I | re.S)
_HEAD_RE = re.compile(r"<head[^>]*>.*?</head>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _clean(text: str) -> str:
    """Strip tags and entities out of an HTML fragment and collapse whitespace."""
    return _WS_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", text))).strip()


def strip_html(body: str) -> str:
    """Body text of an HTML document.

    `<head>` goes first, not just script/style: its `<title>` and `<style>` text would
    otherwise lead the extract, which both pollutes search hits and defeats the
    heading-deduplication that picks a summary.
    """
    return _clean(_SCRIPT_STYLE_RE.sub(" ", _HEAD_RE.sub(" ", body)))


def parse_html(body: str) -> dict:
    """Metadata the artifact-design skill puts in every artifact.

    `title` is the primary field — every artifact in the tree has one. `data-updated` and
    `data-status` are the slice markers the skill requires on phased docs, and they are the
    two most useful filters, so they are indexed rather than re-derived from mtime.
    """
    meta: dict = {}
    m = _TITLE_RE.search(body)
    if m:
        meta["title"] = _clean(m.group(1))
    m = _H1_RE.search(body)
    if m:
        meta["heading"] = _clean(m.group(1))
    m = _UPDATED_RE.search(body)
    if m:
        meta["updated"] = m.group(1).strip()
    m = _META_DESC_RE.search(body)
    if m:
        meta["summary"] = _clean(m.group(1))
    statuses = [s.lower() for s in _SLICE_RE.findall(body)]
    if statuses:
        meta["slices"] = {
            "total": len(statuses),
            "done": statuses.count("done"),
            "active": statuses.count("active"),
            "planned": statuses.count("planned"),
        }
    text = strip_html(body)
    if not meta.get("summary"):
        # Skip the heading itself, so the summary is the first real prose, not a repeat of
        # the title one line above it.
        head = meta.get("heading") or meta.get("title") or ""
        rest = text[len(head) :].strip() if head and text.startswith(head) else text
        meta["summary"] = rest[:240].strip()
    meta["text"] = text[:EXCERPT_CHARS]
    return meta


def parse_markdown(body: str) -> dict:
    """Title from the first ATX heading; summary from the first non-heading line."""
    meta: dict = {}
    lines = [ln.strip() for ln in body.splitlines()]
    for ln in lines:
        if ln.startswith("#"):
            meta["title"] = ln.lstrip("#").strip()
            break
    for ln in lines:
        if ln and not ln.startswith("#"):
            meta["summary"] = ln[:240]
            break
    meta["text"] = _WS_RE.sub(" ", body)[:EXCERPT_CHARS]
    return meta


def slug_title(name: str) -> str:
    """Fallback title: `ansible-k3s-drift-audit_2026-08-16.html` -> `ansible k3s drift audit`."""
    stem = name.rsplit(".", 1)[0]
    stem = re.sub(r"_?\d{4}-\d{2}-\d{2}$", "", stem)
    return stem.replace("-", " ").replace("_", " ").strip() or name


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


def build_index(root: Path) -> dict:
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
    """
    if not host or "/" in host or host in (".", ".."):
        return None
    base = (root / host).resolve()
    try:
        target = (base / rel).resolve()
    except OSError, ValueError:
        return None
    if target != base and base not in target.parents:
        return None
    if not target.is_file():
        return None
    return target


def render_gui() -> bytes:
    return GUI_HTML.encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "artifacts/1.0"
    cache: IndexCache = None  # type: ignore[assignment]

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        # One line per request on stdout, so `kubectl logs` reads like every other workload
        # here rather than BaseHTTPRequestHandler's stderr format.
        print(f"{self.address_string()} {fmt % args}", flush=True)

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

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
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


GUI_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Homelab Artifacts</title>
<style>
:root{--base:#1e1e2e;--mantle:#181825;--crust:#11111b;--surface0:#313244;--surface1:#45475a;
--text:#cdd6f4;--subtext0:#a6adc8;--overlay0:#6c7086;--blue:#89b4fa;--green:#a6e3a1;
--yellow:#f9e2af;--mauve:#cba6f7;--red:#f38ba8}
*{box-sizing:border-box}
body{margin:0;background:var(--base);color:var(--text);
font:15px/1.55 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif}
header{position:sticky;top:0;background:var(--mantle);border-bottom:1px solid var(--surface1);
padding:16px 24px;z-index:5}
h1{margin:0 0 12px;font-size:18px;letter-spacing:.3px}
h1 span{color:var(--overlay0);font-weight:400;font-size:13px;margin-left:8px}
.controls{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
input[type=search]{flex:1 1 320px;min-width:220px;background:var(--crust);color:var(--text);
border:1px solid var(--surface1);border-radius:6px;padding:9px 12px;font-size:14px}
input[type=search]:focus{outline:none;border-color:var(--blue)}
select,label.chk{background:var(--crust);color:var(--subtext0);border:1px solid var(--surface1);
border-radius:6px;padding:8px 10px;font-size:13px}
label.chk{display:flex;align-items:center;gap:6px;cursor:pointer}
main{padding:20px 24px 60px;max-width:1100px;margin:0 auto}
.card{display:block;background:var(--surface0);border:1px solid var(--surface1);border-left:3px solid var(--blue);
border-radius:8px;padding:14px 16px;margin-bottom:10px;text-decoration:none;color:inherit}
.card:hover{border-color:var(--blue);background:#3a3b52}
.card h2{margin:0 0 4px;font-size:15px;font-weight:600;color:var(--blue)}
.card p{margin:0 0 8px;color:var(--subtext0);font-size:13px}
.meta{color:var(--overlay0);font-size:12px;display:flex;gap:14px;flex-wrap:wrap}
.chip{display:inline-block;padding:1px 7px;border-radius:10px;font-size:11px;font-weight:600;
color:var(--crust)}
.chip-host{background:var(--mauve)}.chip-done{background:var(--green)}
.chip-active{background:var(--yellow)}.chip-planned{background:var(--overlay0)}
mark{background:var(--yellow);color:var(--crust);border-radius:2px}
.empty{color:var(--overlay0);padding:40px 0;text-align:center}
footer{color:var(--overlay0);font-size:12px;padding:0 24px 30px;max-width:1100px;margin:0 auto}
</style></head>
<body>
<header>
  <h1>Homelab Artifacts <span id="count"></span></h1>
  <div class="controls">
    <input type="search" id="q" placeholder="Search titles, summaries and body text..." autofocus>
    <select id="host"><option value="">All hosts</option></select>
    <select id="sort">
      <option value="mtime">Newest first</option>
      <option value="title">Title A-Z</option>
      <option value="host">Host</option>
    </select>
    <label class="chk"><input type="checkbox" id="dupes"> show .md duplicates</label>
  </div>
</header>
<main><div id="list"></div></main>
<footer id="foot"></footer>
<script>
let DATA = {artifacts: [], hosts: [], generated: ""};
const $ = (id) => document.getElementById(id);
const esc = (s) => (s || "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

function fmtDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return d.toISOString().slice(0, 16).replace("T", " ") + " UTC";
}
function fmtSize(n) {
  return n < 1024 ? n + " B" : n < 1048576 ? (n / 1024).toFixed(0) + " KB"
                                           : (n / 1048576).toFixed(1) + " MB";
}
// Every whitespace-separated term must appear somewhere in the haystack (AND, not phrase),
// so "drift box" finds the daniel-box drift audit without caring about word order.
function matches(a, terms) {
  if (!terms.length) return true;
  const hay = [a.title, a.summary, a.name, a.host, a.text].join(" ").toLowerCase();
  return terms.every(t => hay.includes(t));
}
function highlight(text, terms) {
  let out = esc(text);
  for (const t of terms) {
    if (!t) continue;
    out = out.replace(new RegExp("(" + t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + ")", "gi"),
                      "<mark>$1</mark>");
  }
  return out;
}
function render() {
  const terms = $("q").value.toLowerCase().split(/\s+/).filter(Boolean);
  const host = $("host").value;
  const sort = $("sort").value;
  const dupes = $("dupes").checked;
  let items = DATA.artifacts.filter(a =>
    (!host || a.host === host) && (dupes || !a.companion_html) && matches(a, terms));
  items.sort((x, y) => sort === "title" ? x.title.localeCompare(y.title)
           : sort === "host" ? (x.host + x.title).localeCompare(y.host + y.title)
           : y.mtime.localeCompare(x.mtime));
  $("count").textContent = items.length + " of " + DATA.artifacts.length + " artifacts";
  $("list").innerHTML = items.length ? items.map(a => {
    const slices = a.slices ? ["done", "active", "planned"].filter(k => a.slices[k])
      .map(k => `<span class="chip chip-${k}">${a.slices[k]} ${k}</span>`).join(" ") : "";
    return `<a class="card" href="${esc(a.url)}" target="_blank" rel="noopener">
      <h2>${highlight(a.title, terms)}</h2>
      ${a.summary ? `<p>${highlight(a.summary, terms)}</p>` : ""}
      <div class="meta">
        <span class="chip chip-host">${esc(a.host)}</span>
        <span>${esc(a.name)}</span>
        <span>${fmtDate(a.updated || a.mtime)}</span>
        <span>${fmtSize(a.size)}</span>
        ${slices}
      </div></a>`;
  }).join("") : `<div class="empty">No artifact matches that search.</div>`;
}
async function load() {
  const res = await fetch("/api/index.json", {cache: "no-store"});
  DATA = await res.json();
  $("host").innerHTML = '<option value="">All hosts</option>' +
    DATA.hosts.map(h => `<option value="${esc(h)}">${esc(h)}</option>`).join("");
  $("foot").textContent = "Indexed " + fmtDate(DATA.generated) +
    " - artifacts are pruned 7 days after their last update.";
  render();
}
["q", "host", "sort", "dupes"].forEach(id => $(id).addEventListener("input", render));
load();
</script>
</body></html>
"""


def main() -> None:
    Handler.cache = IndexCache(ROOT)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(
        f"serving {ROOT} on :{PORT} at {time.strftime('%Y-%m-%d %H:%M:%S')}", flush=True
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
