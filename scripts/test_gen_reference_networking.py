"""Tests for scripts/gen_reference_networking.py.

Fixture-driven: synthetic host_vars and roles under tmp_path.

Run: uv run pytest scripts/test_gen_reference_networking.py
"""

from __future__ import annotations

import textwrap

import gen_reference_networking as g


def _make(tmp_path):
    host_vars = tmp_path / "host_vars"
    host_vars.mkdir()
    (host_vars / "box.yml").write_text(
        textwrap.dedent("""\
        containers_list:
          - name: sonarr
            platform: k8s
            hostname: sonarr
            use_authelia: true
          - name: crowdsec
            platform: k8s
            use_authelia: false
          - name: karakeep
            platform: k8s
            use_authelia: true
          - name: media-volume
            platform: k8s
          - name: dozzle
            port: 8080
        """)
    )
    roles = tmp_path / "roles_k8s"

    def _tpl(role, body):
        path = roles / role / "templates" / "ingressroute.yaml.j2"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(body))

    _tpl("sonarr", "{{ ingressroute(a, b, c, d) }}\n")
    _tpl("crowdsec", "{{ ingressroute(a, b, c, d, public=false) }}\n")
    _tpl(
        "karakeep",
        "{{ ingressroute(a, b, c, d, extra_middlewares=['csp-karakeep']) }}\n",
    )
    # media-volume: an infra role with no ingressroute template at all.
    (roles / "media-volume" / "templates").mkdir(parents=True)
    return host_vars, roles


def test_only_services_with_a_route_appear(tmp_path):
    host_vars, roles = _make(tmp_path)
    names = {r["name"] for r in g.build_rows(host_vars, roles)}
    assert names == {"sonarr", "crowdsec", "karakeep"}


def test_docker_services_are_excluded(tmp_path):
    """A docker host binds to the LAN directly; it has no Traefik route to describe."""
    host_vars, roles = _make(tmp_path)
    assert "dozzle" not in {r["name"] for r in g.build_rows(host_vars, roles)}


def test_public_false_is_reported_as_lan_only(tmp_path):
    host_vars, roles = _make(tmp_path)
    rows = {r["name"]: r for r in g.build_rows(host_vars, roles)}
    assert rows["crowdsec"]["reach"] == "LAN only"
    assert rows["sonarr"]["reach"].startswith("LAN + public")


def test_authelia_middleware_comes_from_the_inventory(tmp_path):
    host_vars, roles = _make(tmp_path)
    rows = {r["name"]: r for r in g.build_rows(host_vars, roles)}
    assert "authelia" in rows["sonarr"]["middlewares"]
    assert "authelia" not in rows["crowdsec"]["middlewares"]


def test_rate_limit_is_on_every_route(tmp_path):
    """The shared macro applies it unconditionally."""
    host_vars, roles = _make(tmp_path)
    for row in g.build_rows(host_vars, roles):
        assert "rate-limit" in row["middlewares"], row["name"]


def test_extra_middlewares_are_picked_up(tmp_path):
    host_vars, roles = _make(tmp_path)
    rows = {r["name"]: r for r in g.build_rows(host_vars, roles)}
    assert "csp-karakeep" in rows["karakeep"]["middlewares"]


def test_hostname_defaults_to_the_service_name(tmp_path):
    host_vars, roles = _make(tmp_path)
    rows = {r["name"]: r for r in g.build_rows(host_vars, roles)}
    assert rows["crowdsec"]["hostname"] == "crowdsec"


def test_markdown_says_the_suffix_is_not_derivable(tmp_path):
    """`domain` is SOPS-sourced, so an FQDN here would be a guess."""
    host_vars, roles = _make(tmp_path)
    out = g.render_markdown(g.build_rows(host_vars, roles))
    assert "SOPS-sourced" in out
    assert "label" in out.lower()


def test_markdown_records_the_authelia_302_trap(tmp_path):
    """A 302 proves the edge is up and nothing about the backend."""
    host_vars, roles = _make(tmp_path)
    out = g.render_markdown(g.build_rows(host_vars, roles))
    assert "302" in out


def test_markdown_opens_with_the_provenance_banner(tmp_path):
    host_vars, roles = _make(tmp_path)
    out = g.render_markdown(g.build_rows(host_vars, roles))
    assert out.startswith("---\n")
    assert "generated_from: scripts/gen_reference_networking.py" in out


def test_markdown_ends_with_exactly_one_newline(tmp_path):
    host_vars, roles = _make(tmp_path)
    out = g.render_markdown(g.build_rows(host_vars, roles))
    assert out.endswith("\n")
    assert not out.endswith("\n\n")
