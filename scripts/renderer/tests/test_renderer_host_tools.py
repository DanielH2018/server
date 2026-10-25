"""`homelab-tools` renders pins, not bytes: one tree per architecture, each tool at a private path.

H3.11 (docs/target-architecture/host/h3-pi-pilot.md, *`homelab-tools`*). The real package holds
`/usr/lib/homelab/bin/sops` and `/usr/lib/homelab/bin/chezmoi` as named members. The spec cannot
name another install path, so the reject fixtures below are the ways a tool could end up
elsewhere or unpinned.

Run: uv run pytest scripts/renderer/tests/test_renderer_host_tools.py
"""

import json

import pytest

from lib import yaml_fast
from renderer.renderer_lib import host as host_mode
from renderer.renderer_lib.cluster import RenderRefused
from renderer.renderer_lib.host_tools import ARCHES, BIN_DIR, PINS_FILE
from lib.repo_paths import REPO

NAMED_MEMBERS = frozenset(
    {f"{BIN_DIR}/{t}" for t in ("sops", "chezmoi", "nfpm", "node-exporter", "uv")}
)
# nfpm and uv name the amd64 build x86_64, and uv names the arm64 build aarch64.
UPSTREAM_ARCH = {"amd64": ("x86_64",), "arm64": ("aarch64",)}
DIGEST = "a" * 64


def missing_named_members(package) -> list[str]:
    """Each named member the package does not install, sorted."""
    return sorted(NAMED_MEMBERS - set(package.installed_paths()))


def pin(version: str = "v1.2.3", **extra) -> dict:
    return {
        "version": version,
        "sha256": DIGEST,
        "url": "https://example.invalid/{version}/x-{bare}",
        **extra,
    }


def spec(**tools: dict) -> dict:
    """A package spec whose tools each pin `pin()` for both architectures."""
    return {"tools": {name: {a: pin() for a in ARCHES} for name in tools or ["sops"]}}


def render(hosts, package: dict, where: str = "packages/homelab-tools"):
    root = hosts(package, where=where)
    name = where.rsplit("/", 1)[-1]
    if where.startswith("packages/"):
        return host_mode.render_package(name, root)
    return host_mode.render_host(name, root)


def test_the_real_package_installs_its_tools_under_the_private_bin_dir():
    package = host_mode.render_package("homelab-tools")
    assert missing_named_members(package) == []
    assert not any(path.startswith("/usr/local/") for path in package.installed_paths())


def test_a_package_that_moves_sops_is_missing_a_named_member(hosts):
    # The fixture installs every other named tool at its path and sops under another tool name.
    package = render(
        hosts,
        {
            "tools": {
                "chezmoi": dict.fromkeys(ARCHES, pin()),
                "nfpm": dict.fromkeys(ARCHES, pin()),
                "node-exporter": dict.fromkeys(ARCHES, pin()),
                "uv": dict.fromkeys(ARCHES, pin()),
                "sops-old": dict.fromkeys(ARCHES, pin()),
            }
        },
    )
    assert missing_named_members(package) == [f"{BIN_DIR}/sops"]


def test_a_render_writes_one_tree_per_architecture_with_its_own_pins(tmp_path):
    out = tmp_path / "out"
    package = host_mode.render_package("homelab-tools")
    host_mode.write_packages([package], out)
    for arch in ARCHES:
        tree = out / "packages" / "homelab-tools" / arch
        nfpm = yaml_fast.safe_load((tree / "nfpm.yaml").read_text())
        assert nfpm["arch"] == arch
        assert {c["dst"] for c in nfpm["contents"]} == NAMED_MEMBERS
        pins = json.loads((tree / PINS_FILE).read_text())
        assert set(pins) == {"sops", "chezmoi", "nfpm", "node-exporter", "uv"}
        assert all(
            arch in p["url"] or any(a in p["url"] for a in UPSTREAM_ARCH[arch])
            for p in pins.values()
        )
        # The render carries pins and no bytes.
        assert not (tree / "root").exists()


def test_a_package_without_tools_keeps_one_tree_and_arch_all(tmp_path):
    out = tmp_path / "out"
    host_mode.write_packages([host_mode.render_package("homelab-updater")], out)
    nfpm = yaml_fast.safe_load((out / "packages/homelab-updater/nfpm.yaml").read_text())
    assert nfpm["arch"] == "all"
    assert not (out / "packages/homelab-updater" / PINS_FILE).exists()


def test_the_pi_package_depends_on_homelab_tools():
    assert "homelab-tools" in host_mode.render_host("daniel-pi").depends


def test_a_valid_fixture_renders_and_expands_both_placeholders(hosts):
    package = render(hosts, spec())
    assert (
        package.tools["sops"]["arm64"].url == "https://example.invalid/v1.2.3/x-1.2.3"
    )


def test_a_member_one_directory_down_renders(hosts):
    package = spec()
    package["tools"]["sops"]["amd64"]["member"] = "uv-x/uv"
    assert render(hosts, package).tools["sops"]["amd64"].member == "uv-x/uv"


def test_a_member_expands_the_version_placeholders(hosts):
    package = spec()
    package["tools"]["sops"]["amd64"]["member"] = "x-{bare}.linux/x"
    assert render(hosts, package).tools["sops"]["amd64"].member == "x-1.2.3.linux/x"


def test_a_member_with_another_placeholder_is_refused(hosts):
    package = spec()
    package["tools"]["sops"]["amd64"]["member"] = "x-{arch}/x"
    with pytest.raises(RenderRefused, match="member"):
        render(hosts, package)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        pytest.param(
            lambda t: t["sops"]["amd64"].update(dst="/usr/local/bin/sops"),
            "an entry holds only",
            id="install-path-on-path",
        ),
        pytest.param(
            lambda t: t["sops"]["amd64"].update(url="http://example.invalid/x"),
            "must be https",
            id="plain-http",
        ),
        pytest.param(
            lambda t: t["sops"]["amd64"].update(url="https://example.invalid/{arch}"),
            "may hold only",
            id="unknown-placeholder",
        ),
        pytest.param(
            lambda t: t["sops"]["amd64"].update(sha256="ABC"),
            "64 lowercase hex",
            id="short-digest",
        ),
        pytest.param(
            lambda t: t["sops"]["amd64"].update(version="latest"),
            "is not vX.Y.Z",
            id="floating-version",
        ),
        pytest.param(
            lambda t: t["sops"]["arm64"].update(version="v1.2.4"),
            "different versions per architecture",
            id="versions-differ",
        ),
        pytest.param(
            lambda t: t["sops"].pop("arm64"),
            "not exactly",
            id="architecture-missing",
        ),
        pytest.param(
            lambda t: t["sops"].update(riscv64=pin()),
            "not exactly",
            id="architecture-unknown",
        ),
        pytest.param(
            lambda t: t["sops"]["amd64"].update(member="../sops"),
            "not a plain file name",
            id="member-escapes",
        ),
        pytest.param(
            lambda t: t["sops"]["amd64"].update(member="a/b/c"),
            "not a plain file name",
            id="member-two-directories-deep",
        ),
        pytest.param(
            lambda t: t["sops"]["amd64"].update(member="a/../sops"),
            "not a plain file name",
            id="member-escapes-after-a-directory",
        ),
        pytest.param(
            lambda t: t.update({"a/b": t.pop("sops")}),
            "not a plain name",
            id="tool-name-with-slash",
        ),
    ],
)
def test_a_tool_spec_that_would_misplace_or_leave_a_binary_unpinned_is_refused(
    hosts, change, message
):
    package = spec()
    change(package["tools"])
    with pytest.raises(RenderRefused, match=message):
        render(hosts, package)


def test_a_host_package_lists_no_tools(hosts):
    with pytest.raises(RenderRefused, match="a host package lists no tools"):
        render(hosts, spec(), where="daniel-pi")


def test_a_file_entry_cannot_install_over_a_tool(hosts):
    package = {
        **spec(),
        "files": [{"src": "x", "dst": f"{BIN_DIR}/sops", "mode": "0755"}],
    }
    root = hosts(package, where="packages/homelab-tools")
    (root / "packages/homelab-tools/x").write_text("x")
    with pytest.raises(RenderRefused, match="two files install"):
        host_mode.render_package("homelab-tools", root)


def test_the_real_spec_pins_one_version_per_tool_with_a_digest_per_architecture():
    raw = yaml_fast.safe_load(
        (REPO / "hosts/packages/homelab-tools/package.yml").read_text()
    )
    for tool, arches in raw["tools"].items():
        assert set(arches) == set(ARCHES), tool
        assert len({a["version"] for a in arches.values()}) == 1, tool
        assert len({a["sha256"] for a in arches.values()}) == len(ARCHES), tool
