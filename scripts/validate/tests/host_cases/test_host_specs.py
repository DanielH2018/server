"""The host package spec loads: each parses its file once, and refuses a malformed one.

Run: uv run pytest scripts/validate/tests/host_cases/test_host_specs.py
"""

from pathlib import Path

import pytest
from lib.repo_paths import REPO
from renderer.renderer_lib.cluster import RenderRefused
from validate.validate_lib.host_specs import load_component, load_nfpm, load_package

HOSTS = REPO / "hosts"
KNOWN_PACKAGES = frozenset(
    {"daniel-box", "daniel-pi", "daniel-server", "homelab-updater"}
)


def _write(tmp_path: Path, directory: str, name: str, text: str) -> Path:
    path = tmp_path / directory / name
    path.parent.mkdir(parents=True)
    path.write_text(text)
    return path


def test_a_package_is_clean(tmp_path):
    spec = load_package(
        _write(tmp_path, "daniel-x", "package.yml", "components: [base, ufw]\n")
    )
    assert (spec.name, spec.components) == ("daniel-x", ("base", "ufw"))


@pytest.mark.parametrize(
    "text",
    ["- base\n", "components: base\n", "components: [{a: b}]\n", "colour: red\n"],
    ids=[
        "not-a-mapping",
        "components-not-a-list",
        "component-not-a-name",
        "unknown-key",
    ],
)
def test_a_malformed_package_is_flagged(tmp_path, text):
    with pytest.raises(RenderRefused):
        load_package(_write(tmp_path, "daniel-x", "package.yml", text))


def test_a_component_is_clean(tmp_path):
    text = (
        "component: owner\n"
        "defaults:\n"
        "  # renovate: datasource=npm depName=tool\n"
        "  tool_version: '1.2'\n"
        "outputs:\n"
        "  - {src: templates/x.j2, keys: [token]}\n"
    )
    spec = load_component(_write(tmp_path, "widget", "component.yml", text))
    assert (spec.name, spec.borrows, dict(spec.defaults), spec.output_keys) == (
        "widget",
        "owner",
        {"tool_version": "1.2"},
        {"token"},
    )
    assert spec.text == text


@pytest.mark.parametrize(
    "text",
    [
        "defaults: [a]\n",
        "component: [owner]\n",
        "outputs: {src: x}\n",
        "outputs: [{src: x, keys: token}]\n",
        "colour: red\n",
    ],
    ids=["defaults", "component", "outputs", "output-keys", "unknown-key"],
)
def test_a_malformed_component_is_flagged(tmp_path, text):
    with pytest.raises(RenderRefused):
        load_component(_write(tmp_path, "widget", "component.yml", text))


def test_an_nfpm_spec_is_clean(tmp_path):
    spec = load_nfpm(
        _write(
            tmp_path,
            "out",
            "nfpm.yaml",
            "name: homelab-x\ncontents:\n  - {src: a, dst: /etc/a, type: file}\n",
        )
    )
    assert (spec.name, spec.contents) == (
        "homelab-x",
        ({"src": "a", "dst": "/etc/a", "type": "file"},),
    )


@pytest.mark.parametrize(
    "text",
    [
        "contents: []\n",
        "name: x\ncontents: {dst: /a}\n",
        "name: x\ncontents: [{src: a}]\n",
    ],
    ids=["no-name", "contents-not-a-list", "entry-without-dst"],
)
def test_a_malformed_nfpm_spec_is_flagged(tmp_path, text):
    with pytest.raises(ValueError, match=r"nfpm\.yaml"):
        load_nfpm(_write(tmp_path, "out", "nfpm.yaml", text))


def test_every_spec_in_the_tree_loads():
    packages = {
        spec.parent.name: load_package(spec)
        for spec in [
            *HOSTS.glob("*/package.yml"),
            *HOSTS.glob("packages/*/package.yml"),
        ]
    }
    components = {
        spec.parent.name: load_component(spec)
        for spec in HOSTS.glob("components/*/component.yml")
    }
    assert KNOWN_PACKAGES <= set(packages), sorted(KNOWN_PACKAGES - set(packages))
    assert "claude-code" in components
