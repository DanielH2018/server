---
name: web-to-rss
description: Subscribe FreshRSS to a blog or site that has no RSS feed by scraping it — find a hidden feed, else write FreshRSS "HTML + XPath" or "XML + XPath" rules, check them against the live page, and hand over an OPML to import. Use when the user says a site "doesn't have RSS", wants to "follow" or "get updates from" a blog, pastes a blog URL to subscribe to, or when a scraped FreshRSS feed stops getting new items or shows an XPath error.
---

Turn a site without a feed into a FreshRSS subscription. FreshRSS (`k8s/roles/freshrss/`)
does the scraping itself on every refresh, so no new service is needed. This skill produces the
scraping rules and proves them before anyone imports them.

`scripts/freshrss/scrape_feed.py` does the mechanical parts. Its two subcommands end on a
machine line: `DISCOVER: verdict=…` and `CHECK: feeds=… failed=…`.

## 1. Discover the source

```bash
uv run python scripts/freshrss/scrape_feed.py discover <url>
```

Pass the page that lists the posts (`…/blog`, not the home page). The verdict picks the branch:

| Verdict | Meaning | Next |
|---|---|---|
| `feed` | The site publishes a feed, advertised or at a common path | Give the user the feed URL to subscribe to normally. Stop here. |
| `html` | The page's static HTML holds the post links | Step 2a |
| `sitemap` | The post list is rendered by JavaScript, but the sitemap lists the posts | Step 2b |
| `none` | Neither | Step 2c |

## 2a. Rules for a static post list (`HTML+XPath`)

Fetch the page with `curl -sL` and save it to the scratchpad, then read the markup around one
post link. FreshRSS evaluates `xPathItem` against the page, and every other rule against each
matched item. All rules are XPath 1.0.

- **Item:** the repeated element that wraps one post. Match a hashed CSS-module class by prefix,
  `//section[contains(@class,'Blog_post')]`, because the suffix changes when the site rebuilds.
- **Title:** `normalize-space(.//h2/a)`.
- **Uri:** make it absolute in the XPath; FreshRSS's docs do not say it resolves relative links.
  `concat(substring('https://site', 1, N * not(starts-with(.//a/@href, 'http'))), .//a/@href)`,
  where N is the length of `https://site`.
- **Uid:** the same expression as Uri. Without one, FreshRSS hashes the item, and a changed
  title or date posts it again.
- **Timestamp:** the date text, cut with `substring-before()` if it shares an element with
  other text. FreshRSS reads it with `strtotime()`; add `frss:xPathItemTimeFormat` (PHP
  `DateTime::createFromFormat`) only when strtotime cannot read it.
- **Author, Content:** optional; use them when the list shows a byline or a summary.

## 2b. Rules from the sitemap (`XML+XPath`)

The sitemap uses a default namespace, so every step uses `local-name()`. The template, with the
blog path filled in:

```
xmlUrl="https://site/sitemap.xml"
frss:xPathItem="//*[local-name()='url'][contains(*[local-name()='loc'],'/blog/') and not(contains(*[local-name()='loc'],'/blog/category'))]"
frss:xPathItemTitle="translate(substring-after(*[local-name()='loc'],'/blog/'),'-',' ')"
frss:xPathItemUri="string(*[local-name()='loc'])"
frss:xPathItemUid="string(*[local-name()='loc'])"
frss:xPathItemTimestamp="string(*[local-name()='lastmod'])"
```

Exclude every listing path the sitemap carries under the blog (categories, tags, pages). Tell
the user the two costs: titles are URL slugs, and `lastmod` is the CMS edit time, not the
publish date.

## 2c. No usable source

Say so, and name what the site needs: a headless-browser scraper such as changedetection.io,
which this homelab does not run. Adding it is a new k3s service (`new-k8s-service` skill), so
ask before starting one. Do not write rules against a page whose raw HTML lacks the posts.

## 3. Write the OPML and check it

Write the outlines into an OPML file in the scratchpad: the `frss` namespace is
`https://freshrss.org/opml`, and each feed is one `<outline text=… type="HTML+XPath"|"XML+XPath"
xmlUrl=… htmlUrl=… frss:xPathItem…=…/>`. Then:

```bash
uv run python scripts/freshrss/scrape_feed.py check <file.opml> --sample 3
```

Done when it prints `failed=0` and the sampled items read right: real titles, links that open
the post, plausible dates. An `error:` line is a broken feed; fix the rule and re-run. A
`warning:` line is a cost to tell the user, not a blocker.

## 4. Hand it over

Send the OPML with `SendUserFile`. The user imports it in FreshRSS under Subscription
management → Import / export, then marks the new feeds read, because the first refresh
delivers every post on the page as unread. Importing into the user's FreshRSS yourself changes
their account, so ask first.

## A scraped feed stopped updating

The site changed its markup. Ask the user to export the feed from FreshRSS (Subscription
management → Import / export → Export), then run `check` on that file. The `error:` line names
the rule that broke. Redo step 1, because the site may now publish a feed or have moved its
posts behind JavaScript.
