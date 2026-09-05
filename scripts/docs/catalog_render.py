#!/usr/bin/env python3
"""The two renderings of the service catalogue: the MkDocs page and the standalone HTML.

Split out of ``scripts/docs/service_catalog.py`` on 2026-09-04. Both read the same rows —
``build_rows`` in the generator stays the only place that reads the tree — so a fact can never
differ between the page and the artifact. The provenance banner and the prose still name
``scripts/docs/service_catalog.py``, because that is the file a reader has to open to find the
FIELD NOTES those sentences point at.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import html

from catalog_model import UNKNOWN, ServiceRow
from lib.docs_provenance import md_cell as _md_cell
from route_facts import linkify_fqdns

__all__ = [
    "render_html",
    "render_markdown",
]


# HTML rendering — Catppuccin Mocha, self-contained, inline CSS.

_CSS = """
:root {
  --base: #1e1e2e; --text: #cdd6f4; --surface0: #313244; --surface1: #45475a;
  --overlay0: #6c7086; --blue: #89b4fa; --green: #a6e3a1; --yellow: #f9e2af;
  --red: #f38ba8; --mauve: #cba6f7; --crust: #11111b;
}
* { box-sizing: border-box; }
body {
  background: var(--base); color: var(--text); font-family: system-ui, sans-serif;
  margin: 0; padding: 2rem;
}
h1 { margin-top: 0; }
p.subtitle { color: var(--overlay0); max-width: 60rem; }
.stats { display: flex; gap: 1rem; flex-wrap: wrap; margin: 1.5rem 0; }
.stat {
  background: var(--surface0); border-radius: 0.5rem; padding: 0.75rem 1rem; min-width: 9rem;
}
.stat .n { font-size: 1.5rem; font-weight: 700; display: block; }
.stat .l { color: var(--overlay0); font-size: 0.8rem; }
h2 { border-bottom: 1px solid var(--surface1); padding-bottom: 0.3rem; margin-top: 2.5rem; }
table { border-collapse: collapse; width: 100%; margin-bottom: 1rem; }
th, td { text-align: left; padding: 0.5rem 0.75rem; border-bottom: 1px solid var(--surface0); }
th { color: var(--overlay0); font-weight: 600; font-size: 0.85rem; text-transform: uppercase; }
tr:hover td { background: var(--surface0); }
.badge {
  display: inline-block; border-radius: 0.35rem; padding: 0.1rem 0.5rem;
  font-size: 0.8rem; font-weight: 600; color: var(--crust);
}
.badge-k8s { background: var(--blue); }
.badge-docker { background: var(--mauve); }
.badge-auth { background: var(--green); }
.badge-noauth { background: var(--yellow); }
.badge-unknown { background: var(--red); }
.badge-na { background: var(--overlay0); }
.muted { color: var(--overlay0); }
"""


def _badge(text: str, cls: str) -> str:
    return f'<span class="badge {cls}">{html.escape(text)}</span>'


def _platform_badge(platform: str) -> str:
    return _badge(platform, "badge-k8s" if platform == "k8s" else "badge-docker")


def _auth_badge(tier: str) -> str:
    if tier.startswith(UNKNOWN):
        return _badge(tier, "badge-unknown")
    if tier == "Authelia":
        return _badge(tier, "badge-auth")
    return _badge(tier, "badge-noauth")


def _autodeploy_badge(text: str) -> str:
    if text.startswith(UNKNOWN):
        cls = "badge-unknown"
    elif text.startswith("eligible"):
        cls = "badge-auth"
    elif text.startswith("n/a"):
        cls = "badge-na"
    else:
        cls = "badge-noauth"
    return _badge(text, cls)


def _count_unknown(rows: list[ServiceRow], field: str) -> int:
    return sum(1 for r in rows if getattr(r, field).startswith(UNKNOWN))


def render_markdown(rows: list[ServiceRow]) -> str:
    """The service catalogue as a MkDocs page, grouped by host.

    A sibling of render_html over the same rows, not a second derivation: build_rows()
    stays the only place that reads the tree.

    Ordering is by host then name rather than by input order. An unstable page would
    make the docs-refresh cron commit on every run.
    """
    from lib.docs_provenance import generated_banner

    hosts = sorted({r.host for r in rows})
    parts = [generated_banner("scripts/docs/service_catalog.py")]
    parts.append("# Services\n")
    parts.append(f"{len(rows)} service(s) declared across {len(hosts)} host(s).\n")

    header = "| Service | Platform | Route | Auth | Backup tier | Auto-deploy |"
    divider = "|---|---|---|---|---|---|"

    for host in hosts:
        host_rows = sorted((r for r in rows if r.host == host), key=lambda r: r.name)
        parts.append(f"\n## {host}\n")
        parts.append(f"{len(host_rows)} service(s).\n")
        parts.append(header)
        parts.append(divider)
        for row in host_rows:
            cells = (
                row.name,
                row.platform,
                # Markdown only. The stored value stays plain text for render_html and
                # every text consumer; only the docs site can resolve these into links.
                linkify_fqdns(row.route),
                row.auth_tier,
                row.backup_tier,
                row.autodeploy,
            )
            parts.append("| " + " | ".join(_md_cell(c) for c in cells) + " |")

    unknowns = sum(
        _count_unknown(rows, field)
        for field in ("route", "auth_tier", "backup_tier", "autodeploy")
    )
    parts.append(
        f"\n## Underivable facts\n\n{unknowns} field(s) read `{UNKNOWN}`. "
        "A fact with no machine-readable source prints its reason rather than a guess — "
        "see the FIELD NOTES section of `scripts/docs/service_catalog.py` for which facts "
        "those are and why.\n"
    )
    # rstrip before the final newline: the parts already end in one, and a file ending
    # "\n\n" is rewritten by the end-of-file-fixer hook. A generated file a hook keeps
    # rewriting would fail the docs-refresh cron's commit on every run.
    return "\n".join(parts).rstrip("\n") + "\n"


def render_html(rows: list[ServiceRow]) -> str:
    """Render `rows` as the standalone HTML service-catalog artifact, one table per host.

    Args:
        rows: Service rows as returned by `build_rows`.

    Returns:
        A complete, self-contained HTML document.
    """
    hosts = sorted({r.host for r in rows})
    total = len(rows)
    unknown_fields = ["route", "auth_tier", "backup_tier", "autodeploy"]
    stats = "".join(
        f'<div class="stat"><span class="n">{_count_unknown(rows, f)}</span>'
        f'<span class="l">unknown {f.replace("_", " ")}</span></div>'
        for f in unknown_fields
    )
    stats = (
        f'<div class="stat"><span class="n">{total}</span><span class="l">services</span></div>'
        + f'<div class="stat"><span class="n">{len(hosts)}</span><span class="l">hosts</span></div>'
        + stats
    )

    sections = []
    for host in hosts:
        host_rows = sorted((r for r in rows if r.host == host), key=lambda r: r.name)
        body = "".join(
            "<tr>"
            f"<td>{html.escape(r.name)}</td>"
            f"<td>{_platform_badge(r.platform)}</td>"
            f'<td class="muted">{html.escape(r.route)}</td>'
            f"<td>{_auth_badge(r.auth_tier)}</td>"
            f'<td class="muted">{html.escape(r.backup_tier)}</td>'
            f"<td>{_autodeploy_badge(r.autodeploy)}</td>"
            "</tr>"
            for r in host_rows
        )
        sections.append(
            f'<h2>{html.escape(host)} <span class="muted">({len(host_rows)} services)</span></h2>'
            "<table><thead><tr><th>Service</th><th>Platform</th><th>Route</th>"
            "<th>Auth</th><th>Backup tier</th><th>Auto-deploy</th></tr></thead>"
            f"<tbody>{body}</tbody></table>"
        )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Homelab Service Catalog</title>
<style>{_CSS}</style>
</head>
<body>
<h1>Homelab Service Catalog</h1>
<p class="subtitle">Generated statically from ansible/inventory and ansible/roles/k8s —
never from a live deploy. "unknown" means the fact genuinely cannot be derived from the
repo alone; see scripts/docs/service_catalog.py's FIELD NOTES for why.</p>
<div class="stats">{stats}</div>
{"".join(sections)}
</body>
</html>
"""
