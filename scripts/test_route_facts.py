"""Tests for scripts/route_facts.py.

Run: uv run pytest scripts/test_route_facts.py
"""

from __future__ import annotations

import route_facts as rf


def _role(tmp_path, name, body=None):
    templates = tmp_path / name / "templates"
    templates.mkdir(parents=True)
    if body is not None:
        (templates / "ingressroute.yaml.j2").write_text(body)
    return tmp_path / name


def _group_vars(tmp_path, text):
    path = tmp_path / "all.yml"
    path.write_text(text)
    return path


def test_a_role_with_no_templates_directory_declares_no_route(tmp_path):
    assert rf.ingressroute_templates(tmp_path / "absent") == []


def test_a_role_with_no_ingressroute_template_declares_no_route(tmp_path):
    role = _role(tmp_path, "media-volume")
    assert rf.ingressroute_templates(role) == []


def test_every_ingressroute_template_is_read_not_just_the_default_name(tmp_path):
    """loki-homelab carries a second route in ingressroute-push.yaml.j2."""
    role = _role(tmp_path, "loki", "{{ ingressroute(a) }}\n")
    (role / "templates" / "ingressroute-push.yaml.j2").write_text("x\n")
    assert len(rf.ingressroute_templates(role)) == 2


def test_public_route_enabled_reads_the_flag(tmp_path):
    assert rf.public_route_enabled(_group_vars(tmp_path, "k8s_public_route: true\n"))
    assert not rf.public_route_enabled(
        _group_vars(tmp_path, "k8s_public_route: false\n")
    )


def test_a_missing_group_vars_file_reads_as_off(tmp_path):
    """Under-reporting reachability is the safe error; over-reporting sends a reader
    to a name that does not resolve."""
    assert not rf.public_route_enabled(tmp_path / "absent.yml")


def test_unparseable_group_vars_reads_as_off(tmp_path):
    assert not rf.public_route_enabled(_group_vars(tmp_path, "k8s_public_route: [\n"))


def test_reachability_is_public_when_the_flag_is_on_and_the_role_opts_in(tmp_path):
    role = _role(tmp_path, "sonarr", "{{ ingressroute(a, b, c, d) }}\n")
    gv = _group_vars(tmp_path, "k8s_public_route: true\n")
    assert rf.reachability(role, gv) == rf.PUBLIC


def test_the_roles_own_opt_out_beats_the_cluster_flag(tmp_path):
    role = _role(tmp_path, "crowdsec", "{{ ingressroute(a, public=false) }}\n")
    gv = _group_vars(tmp_path, "k8s_public_route: true\n")
    assert rf.reachability(role, gv) == rf.LAN


def test_whitespace_around_the_opt_out_is_tolerated(tmp_path):
    """The macro call is hand-written in each role, so spacing varies."""
    role = _role(tmp_path, "crowdsec", "{{ ingressroute(a, public = false) }}\n")
    gv = _group_vars(tmp_path, "k8s_public_route: true\n")
    assert rf.reachability(role, gv) == rf.LAN


def test_route_cell_names_the_public_name_first(tmp_path):
    """It is the one that works from anywhere, so it is the one to reach for."""
    assert (
        rf.route_cell("sonarr", rf.PUBLIC) == "sonarr.<domain> · sonarr.local.<domain>"
    )


def test_route_cell_for_a_lan_only_service_says_so(tmp_path):
    assert rf.route_cell("crowdsec", rf.LAN) == "crowdsec.local.<domain> (LAN only)"


def test_linkify_wraps_each_placeholder_with_its_own_host(tmp_path):
    out = rf.linkify_fqdns(rf.route_cell("sonarr", rf.PUBLIC))
    assert 'data-host="sonarr"' in out
    assert 'data-host="sonarr.local"' in out


def test_linkify_keeps_the_placeholder_as_the_visible_text(tmp_path):
    """A page read outside the docs site has no JavaScript to resolve the span."""
    assert "sonarr.local.&lt;domain&gt;" in rf.linkify_fqdns("sonarr.local.<domain>")


def test_linkify_leaves_ordinary_prose_alone(tmp_path):
    text = "The Cloudflare wildcard resolves any name, so DNS is not the guard."
    assert rf.linkify_fqdns(text) == text


def test_linkify_does_not_match_a_bare_domain_placeholder(tmp_path):
    """`<domain>` with no host in front of it is prose, not an FQDN."""
    assert rf.linkify_fqdns("`domain` is SOPS-sourced") == "`domain` is SOPS-sourced"
