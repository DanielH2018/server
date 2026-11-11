from pathlib import Path

import pytest
from scrape_feed import check, discover

BLOG = "https://example.org/blog"

LIST_PAGE = b"""<html><body>
<section class="Blog_post__a1"><h2><a href="/blog/first">First post</a></h2><p class="meta">Oct 12, 2026</p></section>
<section class="Blog_post__a1"><h2><a href="/blog/second">Second post</a></h2><p class="meta">Aug 5, 2026</p></section>
<section class="Blog_post__a1"><h2><a href="https://elsewhere.net/third">Third post</a></h2><p class="meta">Jul 22, 2026</p></section>
<section class="Blog_post__a1"><h2><a href="/blog/fourth">Fourth post</a></h2><p class="meta">Jul 1, 2026</p></section>
</body></html>"""

SCRIPT_RENDERED_PAGE = b"<html><body><div id='root'></div><script>self.push(['blog/first'])</script></body></html>"

SITEMAP = b"""<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://example.org/blog/first</loc><lastmod>2026-11-10T22:57:28+00:00</lastmod></url>
<url><loc>https://example.org/blog/second</loc><lastmod>2026-11-09T10:00:00+00:00</lastmod></url>
<url><loc>https://example.org/blog/third</loc><lastmod>2026-11-08T10:00:00+00:00</lastmod></url>
<url><loc>https://example.org/pricing</loc><lastmod>2026-11-08T10:00:00+00:00</lastmod></url>
</urlset>"""

RSS = (
    b'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title></channel></rss>'
)


def fake_fetch(pages: dict[str, bytes]):
    def get(url: str) -> bytes:
        if url not in pages:
            raise OSError(f"404 {url}")
        return pages[url]

    return get


@pytest.mark.parametrize(
    ("pages", "verdict"),
    [
        pytest.param(
            {
                BLOG: b'<html><head><link rel="alternate" type="application/rss+xml" href="/blog/rss.xml"></head></html>'
            },
            "feed",
            id="advertised-feed",
        ),
        pytest.param(
            {BLOG: SCRIPT_RENDERED_PAGE, f"{BLOG}/rss.xml": RSS},
            "feed",
            id="unadvertised-feed-path",
        ),
        pytest.param({BLOG: LIST_PAGE}, "html", id="static-post-list"),
        pytest.param(
            # The soft 404 serves HTML with a 200, as baseten.co does for /rss.xml.
            {
                BLOG: SCRIPT_RENDERED_PAGE,
                f"{BLOG}/rss.xml": b"<html>Not found</html>",
                "https://example.org/sitemap.xml": SITEMAP,
            },
            "sitemap",
            id="script-rendered-with-sitemap",
        ),
        pytest.param({BLOG: SCRIPT_RENDERED_PAGE}, "none", id="nothing-usable"),
    ],
)
def test_discover_picks_the_cheapest_source(pages, verdict):
    assert discover(BLOG, fake_fetch(pages))[0] == verdict


def _opml(tmp_path: Path, **rules: str) -> Path:
    attrs = {
        "xPathItem": "//section[contains(@class,'Blog_post')]",
        "xPathItemTitle": "normalize-space(.//h2/a)",
        "xPathItemUri": "concat(substring('https://example.org', 1, 19 * not(starts-with(.//h2/a/@href, 'http'))), .//h2/a/@href)",
        "xPathItemTimestamp": "normalize-space(.//p[contains(@class,'meta')])",
        "xPathItemUid": "string(.//h2/a/@href)",
    } | rules
    frss = " ".join(f'frss:{k}="{v}"' for k, v in attrs.items() if v)
    path = tmp_path / "feeds.opml"
    path.write_text(
        '<opml version="2.0" xmlns:frss="https://freshrss.org/opml"><body>'
        f'<outline text="Example" type="HTML+XPath" xmlUrl="{BLOG}" {frss}/></body></opml>'
    )
    return path


def test_working_rules_are_clean(tmp_path):
    total, failed, lines = check(_opml(tmp_path), fake_fetch({BLOG: LIST_PAGE}))
    assert (total, failed) == (1, 0)
    assert lines == ["1/1 feeds ok"]


@pytest.mark.parametrize(
    ("rules", "error"),
    [
        pytest.param(
            {"xPathItem": "//article"}, "the item XPath matches nothing", id="no-items"
        ),
        pytest.param(
            {"xPathItemTitle": "normalize-space(.//h4)"},
            "4 items with an empty title",
            id="empty-title",
        ),
        pytest.param(
            {"xPathItemUri": "string(.//h2/a/@href)"},
            "3 links are not absolute",
            id="relative-links",
        ),
    ],
)
def test_broken_rules_are_flagged(tmp_path, rules, error):
    total, failed, lines = check(
        _opml(tmp_path, **rules), fake_fetch({BLOG: LIST_PAGE})
    )
    assert (total, failed) == (1, 1)
    assert f"  error: {error}" in lines


def test_unparseable_dates_and_missing_uid_are_warnings_not_failures(tmp_path):
    rules = {"xPathItemTimestamp": "string(.//h2/a)", "xPathItemUid": ""}
    _, failed, lines = check(_opml(tmp_path, **rules), fake_fetch({BLOG: LIST_PAGE}))
    assert failed == 0
    assert "  warning: 4 dates may not parse, e.g. 'First post'" in lines
    assert (
        "  warning: no xPathItemUid: a changed title or date posts the item again"
        in lines
    )
