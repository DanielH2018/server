"""Every vendored upstream manifest matches the sha256 and the URL its role's defaults pin.

`scripts/dev/vendor_manifest.py` holds the contract and does the refresh. A file whose bytes
differ from the pin, a Renovate bump that nobody refreshed, and a file with no pin each fail
here, and the failure names the refresh command.

Run: uv run pytest ansible/tests/k8s/test_vendored_manifests_match_their_pins.py
"""

import hashlib

from lib.repo_paths import ROLES
from vendor_manifest import check_tree, main, roles_in

# (role, file) pairs the census must find in the real tree. traefik-crds vendored the first; H1.3
# adds metallb's `metallb-native.yaml`, and H1.5a adds longhorn's.
KNOWN_VENDORED: frozenset[tuple[str, str]] = frozenset(
    {("traefik-crds", "kubernetes-crd-definition-v1.yml")}
)

UPSTREAM = b"apiVersion: v1\nkind: Namespace\nmetadata:\n  name: demo-system\n"
DEFAULTS = """\
# renovate: datasource=github-releases depName=example/demo
demo_version: {version}
demo_vendored_manifests:
  - name: demo.yaml
    url: "https://example.invalid/{{{{ demo_version }}}}/demo.yaml"
    fetched_url: ""
    sha256: ""
"""


def _vendored_role(tmp_path, version="v1.0.0"):
    """A role the tool itself vendored, from a fake fetch of `UPSTREAM`."""
    role = tmp_path / "k8s" / "demo"
    (role / "defaults").mkdir(parents=True)
    (role / "defaults" / "main.yml").write_text(DEFAULTS.format(version=version))
    code = main(["demo"], roles=tmp_path, fetcher=lambda url: (200, iter([UPSTREAM])))
    assert code == 0
    return role


def test_the_real_tree_holds_every_pin():
    # A broken ROLES path or glob would make check_tree pass over no role at all.
    assert {"flux", "k3s"} <= {role.name for role in roles_in()}
    assert check_tree() == []


def test_the_census_finds_every_known_vendored_file():
    found = {
        (p.parent.parent.parent.name, p.name)
        for p in ROLES.glob("*/*/files/upstream/*")
    }
    assert KNOWN_VENDORED <= found, (
        f"missing from files/upstream/: {KNOWN_VENDORED - found}"
    )


def test_the_tools_own_output_is_clean(tmp_path):
    role = _vendored_role(tmp_path)

    assert (role / "files" / "upstream" / "demo.yaml").read_bytes() == UPSTREAM
    assert check_tree(tmp_path) == []


def test_a_one_byte_change_is_flagged(tmp_path):
    vendored = _vendored_role(tmp_path) / "files" / "upstream" / "demo.yaml"
    vendored.write_bytes(UPSTREAM.replace(b"demo-system", b"demo-systen"))

    [problem] = check_tree(tmp_path)
    assert f"hashes {hashlib.sha256(vendored.read_bytes()).hexdigest()}" in problem
    assert "run scripts/dev/vendor_manifest.py demo" in problem


def test_a_version_bump_without_a_refresh_is_flagged(tmp_path):
    defaults = _vendored_role(tmp_path) / "defaults" / "main.yml"
    defaults.write_text(defaults.read_text().replace("v1.0.0", "v1.1.0", 1))

    [problem] = check_tree(tmp_path)
    assert "url renders https://example.invalid/v1.1.0/demo.yaml" in problem


def test_a_vendored_file_with_no_pin_is_flagged(tmp_path):
    role = _vendored_role(tmp_path)
    (role / "files" / "upstream" / "stray.yaml").write_bytes(UPSTREAM)

    assert check_tree(tmp_path) == [
        "demo: stray.yaml sits in files/upstream/ with no pin in defaults; "
        "run scripts/dev/vendor_manifest.py demo"
    ]
