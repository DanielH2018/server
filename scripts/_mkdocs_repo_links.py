"""MkDocs hook: link a repo path in the prose to where this site documents it.

A page that says `scripts/service_catalog.py` names a thing this site already has a page
about, and left as a bare code span the reader has to go and find it. This hook turns every
such span into a link to the place the docs site documents that path.

WHAT IT LINKS, AND WHY ONLY THAT. Two families, because those are the two the site documents
one-to-one:

  * a script -- `scripts/build_docs.py`, or the bare `build_docs.py` when no other file in
    the tree carries that name -- links to its own row on the Scripts reference page.
  * a docs page -- `docs/secret-rotation.md` -- links to that page.

A role path such as `ansible/roles/k8s/netpol-baseline/tasks/main.yml` is deliberately NOT
linked. The Services page documents *netpol-baseline*: its route, auth, backup tier and
auto-deploy status. It documents nothing about that file, and `tasks/main.yml` and
`defaults/main.yml` would both land on the same row, telling the reader nothing about either.
A link that answers a question the reader did not ask is worse than the plain code span.

WHY A BUILD HOOK AND NOT A SCRIPT IN THE PAGE. `assets/fqdn-links.js` runs in the browser
because the domain it resolves is SOPS-sourced and nothing static can know it. A path -> URL
map has no such problem: both halves are known at build time. Resolving here produces real
anchors in the HTML, costs the reader no JavaScript, and lets a test check every emitted href
against the tree.

WHERE THE ANCHORS COME FROM. The Scripts page is three tables and a Usage section; only 7 of
its 47 scripts have a heading of their own, so a heading id cannot be the target. This hook
injects `id="script-<name>"` onto the Script cell of every row as it rewrites that page, and
`assets/extra.css` highlights the targeted row. The generator is not involved -- one file owns
both halves of the mapping, so an anchor cannot be emitted without a link that reaches it, or
the other way round.

`mkdocs build --strict` does NOT validate what this emits: mkdocs checks markdown links while
rendering, and `on_page_content` runs after that. `scripts/test_mkdocs_repo_links.py` is the
gate.
"""

from __future__ import annotations

import html
import os
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS_PAGE = "reference/scripts.md"

# Directories the basename scan prunes: build output, virtualenvs, other sessions' checkouts,
# and the vendored collections, whose third-party filenames are not ours to disambiguate
# against. Pruning rather than filtering is the difference between 813 ms and 16 ms on a 2.3 s
# build -- `.venv` alone is thousands of files this would otherwise walk and discard.
_SKIP_NAMES = {".git", ".venv", "site", "node_modules", "__pycache__"}
_SKIP_PATHS = {".claude/worktrees", "ansible/collections"}

# The first cell of a generated table row, when that cell is a code span. Every table on the
# Scripts page puts the script filename there.
_TABLE_ROW_NAME = re.compile(r"^\|\s*`([^`]+)`\s*\|")

# Regions whose code spans are left alone. A fenced block shows a command rather than naming a
# file; an anchor already goes somewhere; a heading carries its own permalink, and the Usage
# headings on the Scripts page would otherwise link to a row two screens up.
_PROTECTED = re.compile(
    r"<pre\b[\s\S]*?</pre>|<a\b[\s\S]*?</a>|<h[1-6]\b[\s\S]*?</h[1-6]>", re.IGNORECASE
)

# Only an ATTRIBUTE-FREE code span is a candidate. That is what excludes the Script cells this
# hook has just given an id to -- they would otherwise link to themselves.
_CODE = re.compile(r"<code>([^<]+)</code>")

# The repo writes a reference as `file:line` or `file:line-line`. The line is not addressable
# on this site, so it is stripped for the lookup and kept in the link text.
_LINE_SUFFIX = re.compile(r":\d+(?:-\d+)?$")

# The Script cell of a rendered table row.
_SCRIPT_CELL = re.compile(r"(<tr[^>]*>\s*<td[^>]*>\s*)<code>([^<]+)</code>")


def script_names(markdown: str) -> set[str]:
    """The script filenames `docs/reference/scripts.md` documents, read off its tables."""
    return {
        match.group(1)
        for match in (_TABLE_ROW_NAME.match(line) for line in markdown.splitlines())
        if match
    }


def anchor_for(name: str) -> str:
    """`service_catalog.py` -> `script-service_catalog-py`."""
    return "script-" + name.replace(".", "-")


def shared_basenames(repo: Path, names: set[str]) -> set[str]:
    """Which of `names` also name a file OUTSIDE `scripts/`.

    A bare `probe.py` in the prose is only unambiguous if the tree holds exactly one. This is
    what decides whether the bare form gets a link or only the `scripts/`-prefixed one does.
    """
    scripts_dir = repo / "scripts"
    shared: set[str] = set()
    for directory, subdirectories, filenames in os.walk(repo):
        current = Path(directory)
        relative = current.relative_to(repo).as_posix()
        subdirectories[:] = [
            name
            for name in subdirectories
            if name not in _SKIP_NAMES
            and (name if relative == "." else f"{relative}/{name}") not in _SKIP_PATHS
        ]
        if current == scripts_dir:
            continue
        shared.update(name for name in filenames if name in names)
    return shared


def build_index(
    doc_uris: set[str], scripts: set[str], ambiguous: set[str]
) -> dict[str, tuple[str, str]]:
    """Path as written in the prose -> (the page's src_uri, the anchor on it)."""
    index: dict[str, tuple[str, str]] = {}
    for uri in doc_uris:
        index[f"docs/{uri}"] = (uri, "")
    for name in scripts:
        target = (SCRIPTS_PAGE, anchor_for(name))
        index[f"scripts/{name}"] = target
        if name not in ambiguous:
            index[name] = target
    return index


def lookup(index: dict[str, tuple[str, str]], text: str) -> tuple[str, str] | None:
    """Resolve one code span's text, tolerating a `:line` suffix and a trailing slash."""
    key = _LINE_SUFFIX.sub("", text.strip()).rstrip("/")
    return index.get(key)


def add_script_anchors(page_html: str, names: set[str]) -> str:
    """Give every Script cell on the Scripts page an id, so a link can reach the row."""
    seen: set[str] = set()

    def substitute(match: re.Match[str]) -> str:
        prefix, name = match.group(1), match.group(2)
        if name not in names or name in seen:
            return match.group(0)
        seen.add(name)
        return f'{prefix}<code id="{anchor_for(name)}">{name}</code>'

    return _SCRIPT_CELL.sub(substitute, page_html)


def link_paths(page_html: str, resolve) -> str:
    """Wrap every resolvable code span outside a protected region in a link.

    `resolve` takes the span's text and returns an href, or None to leave it alone.
    """

    def substitute(match: re.Match[str]) -> str:
        href = resolve(html.unescape(match.group(1)))
        if href is None:
            return match.group(0)
        escaped = html.escape(href, quote=True)
        return f'<a class="repo-path" href="{escaped}">{match.group(0)}</a>'

    pieces: list[str] = []
    position = 0
    for protected in _PROTECTED.finditer(page_html):
        pieces.append(_CODE.sub(substitute, page_html[position : protected.start()]))
        pieces.append(protected.group(0))
        position = protected.end()
    pieces.append(_CODE.sub(substitute, page_html[position:]))
    return "".join(pieces)


# --- MkDocs events -------------------------------------------------------------------------

_state: dict[str, object] = {}


def on_files(files, config):  # noqa: ANN001 - mkdocs event signature
    """Build the path index once per build, from the file set mkdocs is about to render."""
    scripts_page = files.get_file_from_path(SCRIPTS_PAGE)
    names: set[str] = set()
    if scripts_page is not None:
        names = script_names(
            Path(scripts_page.abs_src_path).read_text(encoding="utf-8")
        )
    doc_uris = {file.src_uri for file in files.documentation_pages()}
    _state["names"] = names
    _state["index"] = build_index(doc_uris, names, shared_basenames(REPO, names))
    return files


def on_page_content(page_html, page, config, files):  # noqa: ANN001 - mkdocs event signature
    from mkdocs.utils import get_relative_url

    index = _state.get("index")
    if not index:
        return page_html

    if page.file.src_uri == SCRIPTS_PAGE:
        page_html = add_script_anchors(page_html, _state["names"])

    def resolve(text: str) -> str | None:
        found = lookup(index, text)
        if found is None:
            return None
        src_uri, anchor = found
        # A page naming itself with nothing to jump to would link the reader nowhere.
        if src_uri == page.file.src_uri and not anchor:
            return None
        target = files.get_file_from_path(src_uri)
        if target is None:
            return None
        relative = get_relative_url(target.url, page.url)
        return f"{relative}#{anchor}" if anchor else relative

    return link_paths(page_html, resolve)
