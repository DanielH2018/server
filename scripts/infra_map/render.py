"""Rendering: turn the reconciled model into one self-contained HTML page.

Reads the model dict ``infra_map.model`` builds and nothing else — no repo, no
cluster — so a page can be re-rendered from a saved model. All CSS and the SVG
diagram are inlined, because the output has to open over ``file://``.

The views live in siblings and this module composes them: ``infra_map.style`` holds
the stylesheet and the status vocabulary, ``infra_map.html_views`` the host panels,
``infra_map.groups`` the functional grouping, and ``infra_map.diagram`` the
architecture figure. ``group_services`` is re-exported because ``gen_infra_map``
imports it from here.
"""

import sys as _sys
from pathlib import Path as _Path

# `infra_map` is a namespace package under `scripts/`, so reaching a sibling by package
# name needs `scripts/` on sys.path: a directly-invoked script gets only its own directory,
# and pyproject's `pythonpath` is a pytest setting.
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from infra_map.constants import PAGE_REFRESH_SECONDS
from infra_map.diagram import diagram_svg_fragment, diagram_view
from infra_map.groups import group_services
from infra_map.html_views import host_panel
from infra_map.style import STATUS_LABELS, STYLE, e

__all__ = ["group_services", "render_html", "render_svg"]


def _groups_view(model: dict) -> str:
    """A status-coloured chip per service, grouped by what it is for."""
    columns = []
    for group in group_services(model):
        chips = "".join(
            f'<li><span class="dot {e(s["status"])}" title="{e(STATUS_LABELS.get(s["status"], s["status"]))}">'
            f"</span>{e(s['name'])}</li>"
            for s in group["services"]
        )
        columns.append(
            f'<div class="grp"><h4>{e(group["name"])} '
            f'<span class="grp-n">{group["healthy"]}/{len(group["services"])}</span></h4>'
            f"<ul>{chips}</ul></div>"
        )
    return f'<div class="grps">{"".join(columns)}</div>'


def _table_view(model: dict) -> str:
    rows = []
    for service in model["services"]:
        # "Runs on" is where the pods landed; a k8s service with none falls back
        # to the host that declares it.
        runs_on = ", ".join(service.get("nodes") or []) or service.get("host", "")
        rows.append(
            "<tr>"
            f'<td class="mono">{e(service["name"])}</td>'
            f'<td class="mono">{e(runs_on)}</td>'
            f"<td>{e(service['platform'])}</td>"
            f"<td>{e(STATUS_LABELS.get(service['status'], service['status']))}</td>"
            f'<td class="mono">{e(service["hostname"] or "—")}</td>'
            f'<td class="mono">{e(service["image"] or "—")}</td>'
            f"<td>{e(service['detail'] or '—')}</td>"
            "</tr>"
        )
    return (
        "<details><summary>Full service table (sortable by eye, copy-pasteable)</summary>"
        '<div class="scroll"><table><thead><tr>'
        "<th>Service</th><th>Runs on</th><th>Platform</th><th>Status</th>"
        "<th>Hostname</th><th>Image</th><th>Detail</th>"
        f"</tr></thead><tbody>{''.join(rows)}</tbody></table></div></details>"
    )


def render_svg(model: dict) -> str:
    """The architecture diagram as a standalone SVG document, for embedding in Markdown.

    ``diagram_svg_fragment`` draws the whole <svg> element, but two things stop it
    standing alone:

    * Its fills and strokes resolve against the page-level STYLE block. Embedded in a
      Markdown page there is no such block, so the diagram renders as unstyled black
      shapes -- which reads as a broken diagram rather than a missing stylesheet.
      Inlining the CSS as a <style> child makes the element carry its own appearance.
    * A bare <svg> works inside HTML, but a .svg file served on its own is parsed as
      XML and needs xmlns declared.

    The drawing code is untouched. The caption is dropped: the Markdown page around the
    image carries that prose, and a caption baked into the image cannot be edited.
    """
    svg = diagram_svg_fragment(model)
    open_tag_end = svg.index(">") + 1
    open_tag = svg[:open_tag_end].replace(
        "<svg ", '<svg xmlns="http://www.w3.org/2000/svg" ', 1
    )
    # CDATA because the stylesheet contains '>' in descendant selectors, which is not
    # legal bare text in XML.
    # Trailing newline: the end-of-file-fixer prek hook adds one otherwise, and a
    # generated file a hook keeps rewriting fails the docs-refresh cron's commit on
    # every run.
    return f"{open_tag}<style><![CDATA[{STYLE}]]></style>{svg[open_tag_end:]}\n"


def render_html(model: dict) -> str:
    """Render the model to a single self-contained HTML document."""
    totals = model["totals"]
    kpis = [
        ("info", totals["services"], "Services"),
        ("good", totals["healthy"], "Healthy"),
        ("warn", totals["degraded"], "Degraded"),
        ("bad", totals["down"], "Down / missing"),
        ("alt", totals["undeclared"], "Undeclared"),
        ("info", model["cluster"]["pod_count"], "Pods running"),
    ]
    kpi_html = "".join(
        f'<div class="kpi"><div class="n {css}">{value}</div><div class="l">{e(label)}</div></div>'
        for css, value, label in kpis
    )
    legend = "".join(
        f'<span><span class="dot {key}"></span>{label}</span>'
        for key, label in STATUS_LABELS.items()
    )
    hosts_html = "".join(host_panel(h) for h in model["hosts"])

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="{PAGE_REFRESH_SECONDS}">
<title>Homelab infrastructure — daniel-box &amp; daniel-server</title>
<style>{STYLE}</style>
</head><body><div class="wrap">
<h1>Homelab infrastructure</h1>
<p class="lede">Declared state from <code>ansible/inventory/host_vars/</code>, overlaid with
live state from <code>kubectl</code> and <code>docker ps</code>. Regenerated on a timer, so
edits to the inventory and drift in the running fleet both show up here on their own.</p>
<p class="meta">Generated {e(model["generated_at"])} &middot; page reloads every {PAGE_REFRESH_SECONDS // 60} min</p>
<div class="kpis">{kpi_html}</div>
<div class="legend">{legend}</div>

<h2>Architecture</h2>
{diagram_view(model)}

<h2>Workloads by function</h2>
{_groups_view(model)}

<h2>Hosts</h2>
<div class="hosts">{hosts_html}</div>

<h2>All services</h2>
{_table_view(model)}

<footer>
Sources: <code>ansible/inventory/</code> for declared state; live state from
<code>kubectl</code> on daniel-box (deployments, nodes, pods, Longhorn volumes and
backup targets) and one <code>docker ps</code> over ssh to daniel-pi.
The diagram's <em>shape</em> is fixed in <code>scripts/infra_map/diagram.py</code> — those edges
live in role templates, not in the inventory — while its labels, counts and status
colours are read at render time.
Regenerate with <code>uv run python scripts/infra_map/gen_infra_map.py</code>.
</footer>
</div></body></html>
"""
