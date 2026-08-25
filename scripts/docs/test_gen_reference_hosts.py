"""Tests for scripts/docs/gen_reference_hosts.py.

Fixture-driven: a synthetic hosts.ini and host_vars under tmp_path, never the real
inventory, which changes.

Run: uv run pytest scripts/docs/test_gen_reference_hosts.py
"""

from __future__ import annotations

import textwrap

import gen_reference_hosts as g


def _make_inventory(tmp_path):
    ini = tmp_path / "hosts.ini"
    ini.write_text(
        textwrap.dedent("""\
        [homeservers]
        # a comment line
        daniel-server  ansible_connection=local
        daniel-pi   ansible_host=10.0.0.139 ansible_user=ubuntu ansible_connection=ssh
        """)
    )
    host_vars = tmp_path / "host_vars"
    host_vars.mkdir()
    (host_vars / "daniel-server.yml").write_text(
        textwrap.dedent("""\
        server_ip: 10.0.0.161
        has_docker: false
        has_gitops: false
        containers_list:
          - name: a
          - name: b
        """)
    )
    (host_vars / "daniel-pi.yml").write_text(
        textwrap.dedent("""\
        server_ip: 10.0.0.139
        expose_mode: lan
        has_gitops: false
        containers_list:
          - name: c
        """)
    )
    return ini, host_vars


def test_parses_host_lines_with_settings(tmp_path):
    """An ini host line is `name key=value ...`, which configparser reads as one key."""
    ini, _ = _make_inventory(tmp_path)
    hosts = g.parse_hosts_ini(ini)
    assert [h["name"] for h in hosts] == ["daniel-server", "daniel-pi"]
    assert hosts[1]["ansible_host"] == "10.0.0.139"
    assert hosts[1]["ansible_connection"] == "ssh"


def test_skips_comments_and_group_headers(tmp_path):
    ini, _ = _make_inventory(tmp_path)
    names = [h["name"] for h in g.parse_hosts_ini(ini)]
    assert "[homeservers]" not in names
    assert not any(n.startswith("#") for n in names)


def test_counts_services_per_host(tmp_path):
    ini, host_vars = _make_inventory(tmp_path)
    rows = {r["name"]: r for r in g.build_rows(ini, host_vars)}
    assert rows["daniel-server"]["services"] == "2"
    assert rows["daniel-pi"]["services"] == "1"


def test_expose_mode_defaults_are_labelled(tmp_path):
    """A default must read as a default, not as a declared value."""
    ini, host_vars = _make_inventory(tmp_path)
    rows = {r["name"]: r for r in g.build_rows(ini, host_vars)}
    assert rows["daniel-pi"]["expose_mode"] == "lan"
    assert "default" in rows["daniel-server"]["expose_mode"]


def test_an_undeclared_flag_is_unknown_not_false(tmp_path):
    """`has_docker: false` and "nobody said" are different facts.

    Rendering the second as "no" is the guess the unknown convention exists to stop.
    """
    ini, host_vars = _make_inventory(tmp_path)
    missing = tmp_path / "no-group-vars.yml"
    rows = {r["name"]: r for r in g.build_rows(ini, host_vars, missing)}
    assert rows["daniel-server"]["docker"] == "no"
    assert rows["daniel-pi"]["docker"].startswith("unknown")


def test_a_group_default_is_neither_unknown_nor_an_unattributed_yes(tmp_path):
    """A host that declares nothing still inherits group_vars/all.yml, so "absent from
    host_vars" is not "nobody said" -- daniel-pi rendered `unknown (has_docker not
    declared)` while inheriting `has_docker: true`, on the page whose own text calls it the
    only remaining Docker host (2026-08-25 review M-14).

    The provenance survives the fix: a group default must not read as a host assertion.
    """
    ini, host_vars = _make_inventory(tmp_path)
    all_vars = tmp_path / "all.yml"
    all_vars.write_text("has_docker: true\n")
    rows = {r["name"]: r for r in g.build_rows(ini, host_vars, all_vars)}
    assert rows["daniel-pi"]["docker"] == "yes (group default)"
    # An explicit host override still wins, and is still reported bare.
    assert rows["daniel-server"]["docker"] == "no"


def test_a_host_with_no_host_vars_still_renders(tmp_path):
    """A host in the inventory but with no host_vars file must not vanish."""
    ini, host_vars = _make_inventory(tmp_path)
    (host_vars / "daniel-pi.yml").unlink()
    rows = {r["name"]: r for r in g.build_rows(ini, host_vars)}
    assert "daniel-pi" in rows
    assert rows["daniel-pi"]["ip"].startswith("unknown")
    assert rows["daniel-pi"]["services"] == "0"


def test_markdown_opens_with_the_provenance_banner(tmp_path):
    ini, host_vars = _make_inventory(tmp_path)
    out = g.render_markdown(g.build_rows(ini, host_vars))
    assert out.startswith("---\n")
    assert "generated_from: scripts/docs/gen_reference_hosts.py" in out


def test_markdown_records_the_limit_trap(tmp_path):
    """The connection=local pin is the fact an operator most needs from this page."""
    ini, host_vars = _make_inventory(tmp_path)
    out = g.render_markdown(g.build_rows(ini, host_vars))
    assert "-e target=daniel-pi" in out
    assert "--limit" in out


def test_markdown_ends_with_exactly_one_newline(tmp_path):
    """A file the end-of-file-fixer hook rewrites wedges the docs-refresh cron."""
    ini, host_vars = _make_inventory(tmp_path)
    out = g.render_markdown(g.build_rows(ini, host_vars))
    assert out.endswith("\n")
    assert not out.endswith("\n\n")
