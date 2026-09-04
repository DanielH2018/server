"""Metadata extraction and the taxonomy artifact_server.py indexes documents against.

Everything here is a pure function of a document's text plus the taxonomy constants beside
it: what an artifact says about itself (`declared_metadata`), what can be inferred from it
(`derive_category`, `derive_services`, `derive_status`), and the two parsers that turn a file
into an index entry (`parse_html`, `parse_markdown`).

Split out of artifact_server.py, which keeps the HTTP server, the index cache and the path
confinement. That module imports this one; this one imports nothing from it, so the taxonomy
is exercisable without starting a server.

The two tunables a caller varies — `excerpt_chars` and `max_services` — are parameters
defaulting to the constants below, so a test bounds them by argument rather than by patching
this module's globals.
"""

import html
import json
import os
import re
from pathlib import Path

# How much stripped body text goes into the index, so search reaches content and not just
# titles. 2 KB per artifact keeps a ~50-file index well under a megabyte.
EXCERPT_CHARS = int(os.environ.get("ARTIFACTS_EXCERPT_CHARS", "2000"))

# The service names artifacts are matched against, rendered by Ansible from both hosts'
# containers_list plus the retired names older documents still discuss. Absent in a unit test,
# where the fixtures pass their own list.
SERVICES_FILE = Path(
    os.environ.get("ARTIFACTS_SERVICES_FILE", "/app/known_services.json")
)

# A service name is only tagged on a WORD match, never a substring: `nut` occurs inside
# "minute" and "minutes", which appear in nearly every document here — measured 2026-08-19,
# substring matching tagged 33 of 48 artifacts `nut` where word matching tags 16.
#
# Word boundaries are not enough on their own for a name that is also an ordinary noun. These
# need two mentions before the document is about that service, so one passing "the container
# registry" does not tag it.
# `happy` and `tempo` are retired services whose names are ordinary English words, so they sit
# here for the same reason `nut` does.
AMBIGUOUS_SERVICES = {
    "nut",
    "registry",
    "homepage",
    "speedtest",
    "peanut",
    "happy",
    "tempo",
    "foundry",
}

# Keyword -> category, checked against title and body. First category to reach the highest
# score wins; ties fall to the earlier entry here, so the more specific categories lead.
CATEGORY_KEYWORDS = {
    "security": (
        "authelia",
        "crowdsec",
        "sso",
        "vulnerab",
        "cve",
        "secret",
        "sops",
        "permission",
        "rbac",
        "networkpolicy",
        "firewall",
        "auth",
        "credential",
    ),
    "backup": (
        "backup",
        "longhorn",
        "restore",
        "snapshot",
        "b2 ",
        "kopia",
        "retention",
        "disaster",
    ),
    "network": (
        "dns",
        "traefik",
        "ingress",
        "wireguard",
        "pi-hole",
        "pihole",
        "routing",
        "resolv",
        "netplan",
    ),
    "monitoring": (
        "prometheus",
        "grafana",
        "loki",
        "alert",
        "kuma",
        "otel",
        "telemetry",
        "metric",
        "probe",
    ),
    "cost": ("cost", "spend", "cap", "billing", "free tier", "transaction", "pricing"),
    "home-automation": (
        "home assistant",
        "zigbee",
        "automation",
        "z2m",
        "mqtt",
        "lighting",
    ),
    "tooling": ("claude", "skill", "hook", "agent", "session", "artifact", "worktree"),
    "ci": (
        "ci ",
        "github action",
        "renovate",
        "prek",
        "pre-commit",
        "pipeline",
        "gitops",
    ),
    "infra": (
        "k3s",
        "kubernetes",
        "ansible",
        "deploy",
        "cluster",
        "pod",
        "node",
        "docker",
        "role",
    ),
}

# Chosen only when nothing more specific scores — see derive_category.
FALLBACK_CATEGORY = "infra"

# How many service chips a document shows, most-mentioned first. Uncapped, a broad review
# tagged a dozen services and the row stopped carrying information (measured: 5.8 per tagged
# document across the real corpus).
MAX_SERVICES = int(os.environ.get("ARTIFACTS_MAX_SERVICES", "5"))

_META_RE = re.compile(
    r"""<meta[^>]+name=["']artifact:([a-z]+)["'][^>]+content=["']([^"']*)["']""", re.I
)
_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.S)
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


def parse_html(body: str, excerpt_chars: int = EXCERPT_CHARS) -> dict:
    """Metadata the artifact-design skill puts in every artifact.

    `title` is the primary field — every artifact in the tree has one. `data-updated` and
    `data-status` are the slice markers the skill requires on phased docs, and they are the
    two most useful filters, so they are indexed rather than re-derived from mtime.

    Args:
        body: The document's HTML source.
        excerpt_chars: How much stripped body text the `text` field carries.

    Returns:
        The metadata fields found, always including `summary` and `text`.
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
    meta["text"] = text[:excerpt_chars]
    return meta


def load_known_services(path: Path | None = None) -> list[str]:
    """Service names to match documents against; empty when the file is absent."""
    path = path or SERVICES_FILE
    try:
        names = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return []
    return sorted({str(n).lower() for n in names if str(n).strip()})


def split_list(value: str) -> list[str]:
    """Parse a comma- or space-separated metadata value into a clean list."""
    return [
        part.strip().lower()
        for part in re.split(r"[,\s]+", value or "")
        if part.strip()
    ]


def declared_metadata(body: str, is_markdown: bool = False) -> dict:
    """Metadata the artifact states about itself.

    HTML declares it as `<meta name="artifact:category" content="infra">`; Markdown uses YAML
    frontmatter with the same keys. Parsed with a line reader rather than a YAML library
    because the server runs on the standard library alone, and the values are scalars and
    comma lists — not nested YAML.
    """
    found: dict[str, str] = {}
    if is_markdown:
        m = _FRONTMATTER_RE.match(body)
        if m:
            for line in m.group(1).splitlines():
                key, sep, value = line.partition(":")
                if sep and key.strip().islower():
                    found[key.strip()] = value.strip().strip("'\"")
    else:
        found = {k.lower(): v for k, v in _META_RE.findall(body)}

    meta: dict = {}
    if found.get("category"):
        meta["category"] = found["category"].strip().lower()
    if found.get("status"):
        meta["status"] = found["status"].strip().lower()
    for key in ("services", "tags"):
        if found.get(key):
            meta[key] = split_list(found[key])
    return meta


def derive_services(
    text: str, known: list[str], max_services: int = MAX_SERVICES
) -> list[str]:
    """Service names the document is about.

    Word match only, and a name that is also an ordinary English noun needs two mentions —
    see AMBIGUOUS_SERVICES for why one "the container registry" must not tag the document.

    Args:
        text: The document's title and body text, matched case-insensitively.
        known: Candidate service names.
        max_services: How many names to keep, most-mentioned first.

    Returns:
        The matched names, most-mentioned first, capped at `max_services`.
    """
    haystack = text.lower()
    hits = []
    for name in known:
        if len(name) < 3:
            continue
        count = len(re.findall(rf"\b{re.escape(name)}\b", haystack))
        if count >= (2 if name in AMBIGUOUS_SERVICES else 1):
            hits.append((count, name))
    # Most-mentioned first, then capped: a review touching a dozen services is about the two
    # or three it keeps returning to, and a row of twelve chips says less than a row of four.
    # Nothing is lost to search — the names are still in the document text the index carries.
    hits.sort(key=lambda pair: (-pair[0], pair[1]))
    return [name for _, name in hits[:max_services]]


def derive_category(title: str, text: str) -> str:
    """Best-fitting category, by keyword weight.

    Two rules keep this from labelling everything `infra`. The title counts five times a body
    mention, because a document titled "B2 cost review" is about cost however often its body
    says "pod". And `infra` only wins when nothing more specific scores at all: every document
    here describes work on this cluster, so its words appear everywhere and it would otherwise
    swamp the specific categories — measured, it beat `cost` 4-3 on exactly that title.
    """
    title_l = title.lower()
    body = text.lower()
    scores = {
        category: sum(5 * title_l.count(w) + body.count(w) for w in words)
        for category, words in CATEGORY_KEYWORDS.items()
    }
    specific = {c: s for c, s in scores.items() if c != FALLBACK_CATEGORY and s > 0}
    if specific:
        return max(specific, key=lambda c: (specific[c], -list(scores).index(c)))
    return FALLBACK_CATEGORY if scores.get(FALLBACK_CATEGORY) else ""


def derive_status(slices: dict | None) -> str:
    """Document status from the slice chips it already carries.

    Reuses the skill's own `planned|active|done` tokens rather than introducing a second
    vocabulary for the same idea. A document with no slices has no derivable status.
    """
    if not slices:
        return ""
    if slices.get("active"):
        return "active"
    if slices.get("planned"):
        return "planned"
    return "done" if slices.get("done") else ""


def apply_metadata(entry: dict, body: str, is_markdown: bool, known: list[str]) -> None:
    """Merge declared and derived metadata onto an index entry.

    Declared always wins, and every field records which it was. A guessed category that
    renders identically to a stated one would make a heuristic look authoritative — the
    reader has to be able to see which values to trust.
    """
    declared = declared_metadata(body, is_markdown)
    source: dict[str, str] = {}

    for field, derive in (
        (
            "category",
            lambda: derive_category(entry.get("title", ""), entry.get("text", "")),
        ),
        ("status", lambda: derive_status(entry.get("slices"))),
        (
            "services",
            lambda: derive_services(
                f"{entry.get('title', '')} {entry.get('text', '')}", known
            ),
        ),
    ):
        if declared.get(field):
            entry[field] = declared[field]
            source[field] = "declared"
        else:
            value = derive()
            if value:
                entry[field] = value
                source[field] = "derived"

    if declared.get("tags"):
        entry["tags"] = declared["tags"]
        source["tags"] = "declared"
    entry["source"] = source


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
