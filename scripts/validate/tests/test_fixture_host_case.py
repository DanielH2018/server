"""The fixture host's file list, against the real walkthrough and the real renderer (H7.15d).

The container half runs only in CI's `host_packages` job. `bootstrap_case.py` plans it, and
`test_bootstrap_case.py` holds the plan.

Run: uv run pytest scripts/validate/tests/test_fixture_host_case.py
"""

import shutil

import pytest
from lib import yaml_fast
from lib.estate import Inventory
from lib.repo_paths import HOST_VARS, REPO
from renderer.renderer_lib import host as host_mode
from validate.validate_lib import fixture_host_case as case
from validate.validate_lib.host_container import CaseFailed

FILES = (case.WALKTHROUGH, case.EXAMPLE_VARS, case.SCRIPT)


@pytest.fixture
def tree(tmp_path):
    for relative in FILES:
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / relative, tmp_path / relative)
    return tmp_path


def test_the_walkthrough_block_names_the_host_and_the_reconcile_component():
    spec = case.walkthrough_spec((REPO / case.WALKTHROUGH).read_text(), "daniel-new")

    assert "<host>" not in spec
    assert "daniel-new" in spec
    assert yaml_fast.safe_load(spec)["components"] == ["reconcile"]


def test_a_page_without_the_block_is_flagged():
    with pytest.raises(CaseFailed, match="holds no YAML block"):
        case.walkthrough_spec("# A page\n\n## Adding a host\n\nNo block.\n", "x")


def test_the_file_list_adds_the_host_to_the_package_the_vars_and_the_script(tree):
    case.apply_walkthrough(tree)

    host = case.FIXTURE_HOST
    assert (tree / "hosts" / host / "package.yml").is_file()
    host_vars = tree / "inventory/host_vars" / f"{host}.yml"
    assert f"server_ip: {case.FIXTURE_IP}\n" in host_vars.read_text()
    assert f'{host}"\n' in (tree / case.SCRIPT).read_text()


def test_a_script_whose_hosts_line_moved_is_flagged(tree):
    script = tree / case.SCRIPT
    script.write_text(script.read_text().replace('HOSTS="', 'HOST_NAMES="', 1))

    with pytest.raises(CaseFailed, match="HOSTS matched 0 lines"):
        case.apply_walkthrough(tree)


def test_the_walkthrough_host_renders_with_the_real_renderer(tree):
    inventory = tree / "inventory"
    # Templates read other hosts' variables, so the fleet's files stay beside the new one.
    shutil.copytree(
        HOST_VARS,
        inventory / "host_vars",
        dirs_exist_ok=True,
    )
    case.apply_walkthrough(tree)
    hosts_root = tree / "hosts"
    for shared in ("components", "keys", "lib", "packages"):
        (hosts_root / shared).symlink_to(REPO / "hosts" / shared)

    package = host_mode.render_host(
        case.FIXTURE_HOST,
        hosts_root=hosts_root,
        inventory=Inventory(host_vars=inventory / "host_vars"),
    )

    assert package.name == f"homelab-{case.FIXTURE_HOST}"
    assert "homelab-updater.timer" in package.enable_links()
