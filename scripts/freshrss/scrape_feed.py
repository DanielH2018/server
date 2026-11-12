#!/usr/bin/env python3
"""Find a feed source for a site without RSS, and check FreshRSS XPath scraping rules.

`discover URL` looks for the cheapest source a FreshRSS subscription can use, in order: a feed
the site already publishes, post links in the page's static HTML (an "HTML+XPath" feed), and
post URLs in the site's sitemap (an "XML+XPath" feed). It ends on a `DISCOVER: verdict=` line.

`check OPML` fetches every HTML+XPath or XML+XPath outline in a FreshRSS OPML file, runs its
`frss:xPath*` rules with libxml2 (the parser FreshRSS's PHP DOM uses), and reports the feeds
whose items would arrive broken. It ends on a `CHECK: feeds= failed=` line.

The web-to-rss skill (`.claude/skills/web-to-rss/SKILL.md`) is the procedure around both.

Typical usage example:

    uv run python scripts/freshrss/scrape_feed.py discover https://hazyresearch.stanford.edu/blog
    uv run python scripts/freshrss/scrape_feed.py check feeds.opml --sample 3
"""

import json
import sys as _sys
import time
import urllib.request
from collections import Counter
from collections.abc import Callable
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path as _Path
from urllib.parse import urljoin, urlsplit

from lxml import etree, html

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
from lib.cli_help import ArgumentParser
from lib.exit_codes import FAILED, OK

FRSS = "{https://freshrss.org/opml}"
SCRAPE_TYPES = {"HTML+XPath", "XML+XPath"}
FEED_PATHS = ("feed", "rss", "rss.xml", "atom.xml", "feed.xml", "index.xml", "feed/")
MIN_POSTS = 3
LOC = "//*[local-name()='loc']/text()"
DATE_FORMATS = (
    "%b %d, %Y",
    "%B %d, %Y",
    "%d %B %Y",
    "%d %b %Y",
    "%Y-%m-%d",
    "%m/%d/%Y",
)
USER_AGENT = "Mozilla/5.0 (compatible; homelab-scrape-feed)"

Fetch = Callable[[str], bytes]


def fetch(url: str) -> bytes:
    """Return the body at `url`, following redirects; raises on a network or HTTP error."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.read()


def _try_fetch(get: Fetch, url: str) -> bytes | None:
    try:
        return get(url)
    except OSError:
        return None


def is_feed(body: bytes | None) -> bool:
    """Whether `body` is an RSS, Atom or RDF document, whatever Content-Type it was served with."""
    head = (body or b"")[:1000].lstrip().lower()
    return any(tag in head for tag in (b"<rss", b"<feed", b"<rdf:rdf"))


def _strings(root: etree._Element, expr: str) -> list[str]:
    result = root.xpath(expr)
    return (
        [str(x).strip() for x in result if isinstance(x, str)]
        if isinstance(result, list)
        else []
    )


def _elements(root: etree._Element, expr: str) -> list[etree._Element]:
    result = root.xpath(expr)
    return (
        [x for x in result if isinstance(x, etree._Element)]
        if isinstance(result, list)
        else []
    )


def _sitemap_urls(get: Fetch, site: str) -> list[str]:
    robots = _try_fetch(get, urljoin(site, "/robots.txt")) or b""
    maps = [
        line.split(":", 1)[1].strip()
        for line in robots.decode("utf-8", "replace").splitlines()
        if line.lower().startswith("sitemap:")
    ] or [urljoin(site, "/sitemap.xml")]
    locs: list[str] = []
    for index_url in maps[:5]:
        body = _try_fetch(get, index_url)
        if not body:
            continue
        try:
            root = etree.fromstring(body)
        except etree.XMLSyntaxError:
            continue
        found = _strings(root, LOC)
        if etree.QName(root).localname == "sitemapindex":
            for child in found[:20]:
                child_body = _try_fetch(get, child)
                if child_body and child_body.lstrip().startswith(b"<"):
                    locs += _strings(etree.fromstring(child_body), LOC)
        else:
            locs += found
    return locs


def discover(url: str, get: Fetch = fetch) -> tuple[str, list[str]]:
    """Find the cheapest feed source for the page at `url`.

    Returns:
        The verdict (`feed`, `html`, `sitemap` or `none`) and the report lines behind it.
    """
    page = html.fromstring(get(url))
    advertised = [
        urljoin(url, href)
        for href in _strings(
            page,
            "//link[contains(@rel,'alternate')][contains(@type,'rss') or contains(@type,'atom')]/@href",
        )
    ]
    base = url.rstrip("/") + "/"
    candidates = dict.fromkeys(urljoin(base, p) for p in FEED_PATHS)
    candidates.update(dict.fromkeys(urljoin(url, "/" + p) for p in FEED_PATHS))
    probed = [c for c in candidates if is_feed(_try_fetch(get, c))]
    feeds = list(dict.fromkeys(advertised + probed))

    prefix = urlsplit(base).path
    host = urlsplit(url).netloc
    post_links = {
        absolute
        for href in _strings(page, "//a/@href")
        if urlsplit(absolute := urljoin(url, href).split("#")[0]).netloc == host
        and urlsplit(absolute).path.startswith(prefix)
        and urlsplit(absolute).path.rstrip("/") != prefix.rstrip("/")
    }
    in_sitemap = (
        [] if feeds else [u for u in _sitemap_urls(get, url) if u.startswith(base)]
    )

    lines = [
        f"feeds: {len(feeds)} found" + "".join(f"\n  {f}" for f in feeds),
        f"static post links under {prefix}: {len(post_links)}",
    ]
    if not feeds:
        lines.append(f"sitemap URLs under {prefix}: {len(in_sitemap)}")
    if feeds:
        verdict = "feed"
    elif len(post_links) >= MIN_POSTS:
        verdict = "html"
    elif len(in_sitemap) >= MIN_POSTS:
        verdict = "sitemap"
    else:
        verdict = "none"
    return verdict, lines


def _text(value: object) -> str:
    """Reduce an XPath result to the string FreshRSS stores: a node-set gives its first node."""
    if isinstance(value, list):
        if not value:
            return ""
        first = value[0]
        value = first.xpath("string()") if isinstance(first, etree._Element) else first
    if isinstance(value, float):
        value = format(value, "g")
    return " ".join(str(value).split())


def parses_as_date(value: str) -> bool:
    """Whether `value` is in a date shape strtotime() reads, by the common formats only."""
    try:
        datetime.fromisoformat(value)
        return True
    except ValueError:
        pass
    try:
        parsedate_to_datetime(value)
        return True
    except TypeError, ValueError, IndexError:
        pass
    for fmt in DATE_FORMATS:
        try:
            time.strptime(value, fmt)
            return True
        except ValueError:
            continue
    return False


def check_outline(
    outline: etree._Element, body: bytes
) -> tuple[list[dict[str, str]], list[str], list[str]]:
    """Run one outline's rules over `body`.

    Returns:
        The extracted items, the errors that break the feed, and the warnings that do not.
    """
    rules = {
        name: rule
        for name in ("Title", "Uri", "Content", "Author", "Timestamp", "Uid")
        if (rule := outline.get(f"{FRSS}xPathItem{name}"))
    }
    root = (
        html.fromstring(body)
        if outline.get("type") == "HTML+XPath"
        else etree.fromstring(body)
    )
    nodes = _elements(root, outline.get(f"{FRSS}xPathItem") or "/..")
    items = [
        {name: _text(node.xpath(rule)) for name, rule in rules.items()}
        for node in nodes
    ]

    errors: list[str] = []
    warnings: list[str] = []
    if not items:
        errors.append("the item XPath matches nothing")
    for field in ("Title", "Uri"):
        if field not in rules:
            errors.append(f"no xPathItem{field} rule")
            continue
        empty = sum(1 for i in items if not i[field])
        if empty:
            errors.append(f"{empty} items with an empty {field.lower()}")
    relative = sum(
        1
        for i in items
        if i.get("Uri") and not i["Uri"].startswith(("http://", "https://"))
    )
    if relative:
        errors.append(f"{relative} links are not absolute")
    if "Uid" in rules:
        dupes = sum(n - 1 for n in Counter(i["Uid"] for i in items).values() if n > 1)
        if dupes:
            warnings.append(f"{dupes} duplicate uids; FreshRSS keeps the first")
    else:
        warnings.append("no xPathItemUid: a changed title or date posts the item again")
    if "Timestamp" in rules and not outline.get(f"{FRSS}xPathItemTimeFormat"):
        bad = [i["Timestamp"] for i in items if not parses_as_date(i["Timestamp"])]
        if bad:
            warnings.append(f"{len(bad)} dates may not parse, e.g. {bad[0]!r}")
    return items, errors, warnings


def check(
    opml: _Path, get: Fetch = fetch, sample: int = 0
) -> tuple[int, int, list[str]]:
    """Check every scraping outline in `opml`.

    Returns:
        The number of scraping feeds, how many failed, and the report lines. A healthy feed is
        counted but not listed unless `sample` asks for its items.
    """
    outlines = [
        o
        for o in etree.parse(str(opml)).iter("outline")
        if o.get("type") in SCRAPE_TYPES
    ]
    failed = 0
    lines: list[str] = []
    for outline in outlines:
        name = outline.get("text") or outline.get("xmlUrl")
        try:
            items, errors, warnings = check_outline(
                outline, get(outline.get("xmlUrl", ""))
            )
        except (OSError, ValueError, etree.LxmlError) as exc:
            items, errors, warnings = [], [f"fetch or parse failed: {exc}"], []
        failed += bool(errors)
        if errors or warnings or sample:
            lines.append(f"{name}: {len(items)} items" + (" FAILED" if errors else ""))
            lines += [f"  error: {e}" for e in errors] + [
                f"  warning: {w}" for w in warnings
            ]
            lines += [
                f"  item: {json.dumps(item, ensure_ascii=False)}"
                for item in items[:sample]
            ]
    lines.insert(0, f"{len(outlines) - failed}/{len(outlines)} feeds ok")
    return len(outlines), failed, lines


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(
        description=__doc__.split("\n\n")[0],
        example="scrape_feed.py discover https://example.org/blog",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    find = sub.add_parser("discover", help="find a feed source for a page")
    find.add_argument("url")
    test = sub.add_parser(
        "check", help="run an OPML file's XPath rules against the live pages"
    )
    test.add_argument("opml", type=_Path)
    test.add_argument(
        "--sample", type=int, default=0, help="print the first N items of each feed"
    )
    args = parser.parse_args(argv)

    if args.command == "discover":
        verdict, lines = discover(args.url)
        print("\n".join(lines))
        print(f"DISCOVER: verdict={verdict}")
        return OK if verdict != "none" else FAILED
    total, failed, lines = check(args.opml, sample=args.sample)
    print("\n".join(lines))
    print(f"CHECK: feeds={total} failed={failed}")
    return FAILED if failed or not total else OK


if __name__ == "__main__":
    raise SystemExit(main())
