"""Tests for scripts/docs/reference/hosts.py.

Fixture-driven: a synthetic service list and host_vars under tmp_path, never the real
inventory, which changes.

Run: uv run pytest scripts/docs/tests/test_gen_reference_hosts.py
"""

import re
import textwrap

from docs.reference import hosts as g
from lib.estate import Inventory


def _make_inventory(tmp_path):
    host_vars = tmp_path / "host_vars"
    host_vars.mkdir()
    (host_vars / "daniel-box.yml").write_text(
        textwrap.dedent("""\
        server_ip: 10.0.0.161
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
    return Inventory(host_vars=host_vars, services=services)


def test_counts_services_per_host(tmp_path):
    inventory = _make_inventory(tmp_path)
    rows = {r["name"]: r for r in g.build_rows(inventory)}
    assert rows["daniel-box"]["services"] == "2"
    assert rows["daniel-pi"]["services"] == "1"


def test_the_docker_fact_follows_whether_the_package_lists_pi_docker(tmp_path):
    """A host with no package is unknown, not "no": nothing says what it ships."""
    inventory = _make_inventory(tmp_path)
    hosts_root = tmp_path / "hosts"
    (hosts_root / "daniel-pi").mkdir(parents=True)
    (hosts_root / "daniel-pi" / "package.yml").write_text("components: [pi-docker]\n")
    (hosts_root / "daniel-box").mkdir()
    (hosts_root / "daniel-box" / "package.yml").write_text("components: [sshd]\n")
    (inventory.host_vars / "daniel-server.yml").write_text("server_ip: 10.0.0.162\n")
    rows = {
        r["name"]: r["docker"] for r in g.build_rows(inventory, hosts_root=hosts_root)
    }
    assert rows["daniel-pi"] == "yes"
    assert rows["daniel-box"] == "no"
    assert rows["daniel-server"].startswith("unknown")


def test_a_host_whose_host_vars_declare_nothing_still_renders(tmp_path):
    """A host file that declares no address or services must not vanish."""
    inventory = _make_inventory(tmp_path)
    (inventory.host_vars / "daniel-pi.yml").write_text("{}\n")
    rows = {r["name"]: r for r in g.build_rows(inventory)}
    assert "daniel-pi" in rows
    assert rows["daniel-pi"]["ip"].startswith("unknown")
    assert rows["daniel-pi"]["services"] == "0"


def test_markdown_opens_with_the_provenance_banner(tmp_path):
    inventory = _make_inventory(tmp_path)
    out = g.render_markdown(g.build_rows(inventory))
    assert out.startswith("---\n")
    assert "generated_from: scripts/docs/reference/hosts.py" in out


def test_markdown_ends_with_exactly_one_newline(tmp_path):
    """A file the end-of-file-fixer hook rewrites wedges the docs-refresh cron."""
    inventory = _make_inventory(tmp_path)
    out = g.render_markdown(g.build_rows(inventory))
    assert out.endswith("\n")
    assert not out.endswith("\n\n")


def test_the_page_names_each_hosts_package_and_no_inventory_connection_key(tmp_path):
    inventory = _make_inventory(tmp_path)
    hosts_root = tmp_path / "hosts"
    (hosts_root / "daniel-pi").mkdir(parents=True)
    (hosts_root / "daniel-pi" / "package.yml").write_text("components: [a, b]\n")
    rows = g.build_rows(inventory, hosts_root=hosts_root)
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
