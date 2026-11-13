"""Tests for scripts/docs/reference/hosts.py.

Fixture-driven: a synthetic service list and host_vars under tmp_path, never the real
inventory, which changes.

Run: uv run pytest scripts/docs/tests/test_gen_reference_hosts.py
"""

import re
import textwrap

from docs.reference import hosts as g


def _make_inventory(tmp_path):
    host_vars = tmp_path / "host_vars"
    host_vars.mkdir()
    (host_vars / "daniel-box.yml").write_text(
        textwrap.dedent("""\
        server_ip: 10.0.0.161
        has_docker: false
        """)
    )
    (host_vars / "daniel-pi.yml").write_text(
        textwrap.dedent("""\
        server_ip: 10.0.0.139
        compose_services:
          - name: c
        """)
    )
    services = tmp_path / "services.yml"
    services.write_text("containers_list:\n  - name: a\n  - name: b\n")
    return host_vars, services


def test_counts_services_per_host(tmp_path):
    host_vars, services = _make_inventory(tmp_path)
    rows = {r["name"]: r for r in g.build_rows(host_vars, services=services)}
    assert rows["daniel-box"]["services"] == "2"
    assert rows["daniel-pi"]["services"] == "1"


def test_an_undeclared_flag_is_unknown_not_false(tmp_path):
    """`has_docker: false` and "nobody said" are different facts.

    Rendering the second as "no" is the guess the unknown convention exists to stop.
    """
    host_vars, services = _make_inventory(tmp_path)
    missing = tmp_path / "no-group-vars.yml"
    rows = {r["name"]: r for r in g.build_rows(host_vars, missing, services=services)}
    assert rows["daniel-box"]["docker"] == "no"
    assert rows["daniel-pi"]["docker"].startswith("unknown")


def test_a_group_default_is_neither_unknown_nor_an_unattributed_yes(tmp_path):
    """A host that declares nothing still inherits group_vars/all.yml, so "absent from
    host_vars" is not "nobody said" -- daniel-pi rendered `unknown (has_docker not
    declared)` while inheriting `has_docker: true`, on the page whose own text calls it the
    only remaining Docker host (2026-08-25 review M-14).

    The provenance survives the fix: a group default must not read as a host assertion.
    """
    host_vars, services = _make_inventory(tmp_path)
    all_vars = tmp_path / "all.yml"
    all_vars.write_text("has_docker: true\n")
    rows = {r["name"]: r for r in g.build_rows(host_vars, all_vars, services=services)}
    assert rows["daniel-pi"]["docker"] == "yes (group default)"
    # An explicit host override still wins, and is still reported bare.
    assert rows["daniel-box"]["docker"] == "no"


def test_a_host_whose_host_vars_declare_nothing_still_renders(tmp_path):
    """A host file that declares no address or services must not vanish."""
    host_vars, services = _make_inventory(tmp_path)
    (host_vars / "daniel-pi.yml").write_text("{}\n")
    rows = {r["name"]: r for r in g.build_rows(host_vars, services=services)}
    assert "daniel-pi" in rows
    assert rows["daniel-pi"]["ip"].startswith("unknown")
    assert rows["daniel-pi"]["services"] == "0"


def test_markdown_opens_with_the_provenance_banner(tmp_path):
    host_vars, services = _make_inventory(tmp_path)
    out = g.render_markdown(g.build_rows(host_vars, services=services))
    assert out.startswith("---\n")
    assert "generated_from: scripts/docs/reference/hosts.py" in out


def test_markdown_ends_with_exactly_one_newline(tmp_path):
    """A file the end-of-file-fixer hook rewrites wedges the docs-refresh cron."""
    host_vars, services = _make_inventory(tmp_path)
    out = g.render_markdown(g.build_rows(host_vars, services=services))
    assert out.endswith("\n")
    assert not out.endswith("\n\n")


def test_the_page_tells_no_one_to_run_a_playbook(tmp_path):
    """No playbook remains: a host's setup ships in its host package."""
    host_vars, services = _make_inventory(tmp_path)
    out = g.render_markdown(g.build_rows(host_vars, services=services))
    assert "playbook" not in out.lower()


def test_the_page_names_each_hosts_package_and_no_inventory_connection_key(tmp_path):
    host_vars, services = _make_inventory(tmp_path)
    hosts_root = tmp_path / "hosts"
    (hosts_root / "daniel-pi").mkdir(parents=True)
    (hosts_root / "daniel-pi" / "package.yml").write_text("components: [a, b]\n")
    rows = g.build_rows(host_vars, hosts_root=hosts_root, services=services)
    by_name = {r["name"]: r for r in rows}
    assert by_name["daniel-pi"]["package"] == "`homelab-daniel-pi` (2 components)"
    assert by_name["daniel-box"]["package"].startswith("unknown")
    page = g.render_markdown(rows)
    assert "| Host package | `homelab-daniel-pi` (2 components) |" in page
    assert "ansible_connection" not in page


def test_every_live_host_names_its_package_with_a_nonzero_component_count():
    """Named members: a moved `hosts/` tree or `components` key fails here, not silently."""
    packages = {r["name"]: r["package"] for r in g.build_rows()}
    assert set(packages) == {"daniel-box", "daniel-pi", "daniel-server"}
    for name, package in packages.items():
        assert re.fullmatch(rf"`homelab-{name}` \([1-9]\d* components\)", package), name
