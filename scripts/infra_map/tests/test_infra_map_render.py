"""Tests for the rendered page: the HTML map, the topology diagram and the standalone SVG.

The model these render is built by `model_for` from the shared records, so every case
here is about what reaches the page: that it is self-contained, that inventory values
are escaped, that an uncollected cluster is drawn as unknown rather than as down, and
that a disarmed backup target is named.

Run: uv run pytest scripts/infra_map/tests/test_infra_map_render.py
"""

from __future__ import annotations


import gen_infra_map as g
import infra_map_render as render

from _infra_map import (
    GLOBALS,
    ROLES,
    container,
    docker_host,
    live_ok,
    model_for,
)


def rendered():
    return g.render_html(
        model_for(
            {
                "daniel-box": live_ok(
                    {("homelab", "traefik"): {"ready": 1, "desired": 1, "image": "t:1"}}
                ),
                "daniel-pi": live_ok({"sonarr": container()}),
            }
        )
    )


def test_render_html_is_self_contained():
    """It is opened over file://, so any external asset is a broken page."""
    page = rendered()
    for marker in ("http://", "https://", "<script", "src="):
        assert marker not in page, f"page must not reference {marker}"


def test_render_html_includes_both_hosts_and_their_services():
    page = rendered()
    for expected in ("daniel-box", "daniel-server", "sonarr", "traefik"):
        assert expected in page


def test_render_html_escapes_values_from_the_inventory():
    host_vars = {
        "daniel-box": docker_host([]),
        "daniel-server": docker_host([{"name": "<script>evil</script>", "port": 1}]),
    }
    page = g.render_html(g.build_model(GLOBALS, host_vars, {}, "now", ROLES))
    assert "<script>evil" not in page
    assert "&lt;script&gt;evil" in page


def test_render_html_names_an_unreachable_host_in_the_page():
    model = model_for(
        {
            "daniel-box": {"ok": False, "data": {}, "error": "ssh timed out"},
            "daniel-pi": live_ok({"sonarr": container()}),
        }
    )
    assert "ssh timed out" in g.render_html(model)


def test_the_diagram_is_well_formed_svg():
    """Hand-authored markup — one stray tag would break the whole page."""
    from xml.etree import ElementTree

    figure = render._diagram_view(model_for({}))
    fragment = figure[figure.index("<svg") : figure.index("</svg>") + len("</svg>")]
    ElementTree.fromstring(fragment)


def test_the_diagram_labels_edges_with_addresses_from_the_inventory():
    """The reason it is generated: renaming a VIP must move the label."""
    global_vars = {
        **GLOBALS,
        "k3s_metallb_ingress_vip": "10.9.9.9",
        "domain": "example.test",
    }
    model = g.build_model(
        global_vars, {"daniel-box": docker_host([])}, {}, "now", ROLES
    )
    diagram = render._diagram_view(model)
    assert "10.9.9.9" in diagram
    assert "example.test" in diagram


def test_the_diagram_reports_a_disarmed_backup_target():
    """A target with no URL rendering as healthy is the miss worth guarding."""
    cluster = {
        "ok": True,
        "error": "",
        "nodes": {},
        "pods": [],
        "volumes": 0,
        "backup_targets": [
            {"name": "r2", "url": "", "armed": False, "available": False}
        ],
    }
    assert "disarmed" in render._diagram_view(model_for({}, cluster))


def test_an_uncollected_cluster_does_not_claim_the_backups_are_disarmed():
    """Disarmed is a deliberate state here — a failed query must not announce it."""
    diagram = render._diagram_view(model_for({}))
    assert "disarmed" not in diagram
    assert "not collected" in diagram


def test_an_uncollected_cluster_does_not_paint_its_nodes_down():
    """Same false alarm on the nodes: unknown is not NotReady."""
    diagram = render._diagram_view(model_for({}))
    assert "s-down" not in diagram
    assert "s-unknown" in diagram


def test_the_storage_plane_is_not_reddened_by_a_route_only_service():
    """longhorn-ui declares an IngressRoute; its Deployment is the chart's, in
    another namespace, so the name lookup reports it missing. The storage plane
    must not inherit that — it is read from the volumes."""
    cluster = {
        "ok": True,
        "error": "",
        "nodes": {},
        "pods": [],
        "volumes": 42,
        "backup_targets": [],
    }
    diagram = render._diagram_view(model_for({}, cluster))
    longhorn = diagram[diagram.index('y="706"') - 60 : diagram.index('y="706"')]
    assert "s-missing" not in longhorn


def test_a_services_node_placement_reaches_the_page():
    """Placement is collected for a reason; an untagged service hides it."""
    service = {
        "name": "sonarr",
        "status": "healthy",
        "hostname": None,
        "port": None,
        "authelia": False,
        "networks": [],
        "namespace": "homelab",
        "declared": True,
        "detail": "",
        "nodes": ["daniel-server"],
    }
    assert "daniel-server" in render._service_row(service)


def _svg():
    return g.render_svg(
        model_for(
            {
                "daniel-box": live_ok(
                    {("homelab", "traefik"): {"ready": 1, "desired": 1, "image": "t:1"}}
                ),
                "daniel-pi": live_ok({"sonarr": container()}),
            }
        )
    )


def test_svg_is_a_standalone_document():
    out = _svg()
    assert out.lstrip().startswith("<svg")
    assert out.rstrip().endswith("</svg>")
    assert "<figure" not in out
    assert "<html" not in out.lower()


def test_svg_carries_its_own_styles():
    """The whole point of the task.

    _diagram_view's colours come from the page-level STYLE block. Embedded in
    Markdown there is no page, so an SVG without an inline <style> renders as
    unstyled black boxes -- which looks like a broken diagram, not a missing
    stylesheet.
    """
    out = _svg()
    assert "<style" in out
    style_block = out.split("<style")[1].split("</style>")[0]
    assert ".box" in style_block
    assert ".edge" in style_block


def test_svg_declares_the_xml_namespace():
    """A bare <svg> works inside HTML; a .svg served on its own is parsed as XML."""
    assert 'xmlns="http://www.w3.org/2000/svg"' in _svg()


def test_svg_declares_a_viewbox():
    """Without a viewBox an embedded SVG does not scale to its container."""
    assert 'viewBox="' in _svg()


def test_svg_keeps_the_status_tinting():
    """Live status is the reason this diagram beats a static one."""
    assert "s-" in _svg(), "no status classes on any node"


def test_svg_parses_as_xml():
    """An SVG that does not parse renders as nothing, with no error anywhere."""
    import xml.etree.ElementTree as ET

    ET.fromstring(_svg())


def test_svg_ends_with_exactly_one_newline():
    """A file the end-of-file-fixer prek hook rewrites breaks the docs-refresh cron.

    That cron commits generated files with hooks running, so a hook that modifies
    one aborts the commit on every run until the generator is fixed. Canonical
    output at the source is what stops that.
    """
    out = _svg()
    assert out.endswith("\n")
    assert not out.endswith("\n\n")
