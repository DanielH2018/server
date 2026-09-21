"""Guard: every input to the code-server image build is pinned (#2149).

The image is built in-cluster from `templates/Dockerfile.j2`, and until 2026-09-21 it resolved
six inputs from the network at build time: the base tag without a digest, NodeSource's
`setup_22.x` script piped to bash, bare `pip install` names, an unversioned `npm install -g`,
and the CURRENT version of each VS Code extension from Open VSX and the marketplace — which
for a platform-specific extension was whichever build the API listed first (alpine-arm64 for
ruff and ty, win32-x64 for Claude Code). Each guard here names one of those inputs, so the
failure says which one floated again rather than that a count moved.

Run: uv run pytest ansible/tests/services/test_code_server_build_is_pinned.py
"""

import re

import pytest
from jinja2 import Environment

from _helpers import K8S_ROLES, load_defaults

ROLE = K8S_ROLES / "code-server"
DOCKERFILE = ROLE / "templates" / "Dockerfile.j2"

# The extensions the pin table must carry, so a renamed or emptied list fails as a missing
# member rather than passing over nothing. The second set names the ones published per
# platform: their URL has to select the linux-x64 build, or the API's first-listed one ships.
KNOWN_EXTENSIONS = frozenset(
    {
        "ms-python.python",
        "charliermarsh.ruff",
        "astral-sh.ty",
        "ms-python.vscode-pylance",
        "Anthropic.claude-code",
    }
)
PLATFORM_SPECIFIC = frozenset(
    {"charliermarsh.ruff", "astral-sh.ty", "Anthropic.claude-code"}
)
TARGET_PLATFORM = "linux-x64"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RELEASE = re.compile(r"^\d+\.\d+\.\d+$")

# The network-resolving forms the old step used. Any one of them back in the Dockerfile means a
# version is decided at build time again.
FLOATING_FORMS = (
    "deb.nodesource.com",
    "| bash",
    "extensionquery",
    "/api/${publisher}",
)


def extension_pin_problems(entries: list[dict]) -> list[str]:
    """Every entry missing a release version, a sha256, or the platform its URL must name.

    Pure over the parsed list so the red half below can hand it a broken entry without editing
    the tree.
    """
    problems: list[str] = []
    for e in entries:
        name = e.get("name", "<unnamed>")
        version = str(e.get("version", ""))
        if not _RELEASE.match(version):
            problems.append(f"{name}: version {version!r} is not an exact release")
        if not _SHA256.match(str(e.get("sha256", ""))):
            problems.append(f"{name}: sha256 is not 64 hex characters")
        url = str(e.get("url", ""))
        if version and version not in url:
            problems.append(f"{name}: url does not name version {version}")
        if name in PLATFORM_SPECIFIC and TARGET_PLATFORM not in url:
            problems.append(f"{name}: url does not select the {TARGET_PLATFORM} build")
    return problems


def floating_pip_packages(dockerfile: str) -> list[str]:
    """Every package in a `pip install` continuation block without an exact `==` pin."""
    floating: list[str] = []
    block = re.search(r"pip install[^\n]*\\\n((?:\s+\S+\s*\\\n)+)", dockerfile)
    if block is None:
        return ["<no pip install block found>"]
    for line in block.group(1).splitlines():
        token = line.strip().rstrip("\\").strip()
        if token and not token.startswith("-") and "==" not in token:
            floating.append(token)
    return floating


def _rendered_dockerfile() -> str:
    """The Dockerfile as image-builder's `template` lookup renders it.

    `trim_blocks=True` is Ansible's default and a bare `Environment()` is not; the block
    loop's line endings are the one place the two differ. A default that references another
    (`code_server_k8s_node_url` names the version key) is resolved first, as Ansible's lazy
    templating does — a one-pass render would leave the inner `{{ }}` in the output.
    """
    env = Environment(trim_blocks=True)
    defaults = load_defaults(ROLE)
    resolved = {
        key: env.from_string(value).render(**defaults)
        if isinstance(value, str)
        else value
        for key, value in defaults.items()
    }
    context = dict(resolved, puid=1000, pgid=1000)
    return env.from_string(DOCKERFILE.read_text()).render(**context)


def test_the_base_image_carries_a_digest() -> None:
    m = re.search(r"^FROM\s+(\S+)", DOCKERFILE.read_text(), re.MULTILINE)
    assert m, "no FROM line"
    assert re.search(r":\d+\.\d+\.\d+-ls\d+@sha256:[0-9a-f]{64}$", m.group(1)), (
        f"FROM {m.group(1)} must pin the -lsNN release tag AND its digest: linuxserver "
        "rebuilds a tag in place, so the tag alone names a moving image"
    )


def test_every_extension_is_pinned_by_version_and_sha256() -> None:
    entries = load_defaults(ROLE)["code_server_k8s_extensions"]
    names = {e["name"] for e in entries}
    missing = KNOWN_EXTENSIONS - names
    assert not missing, f"extension pin table lost {sorted(missing)}"
    assert not extension_pin_problems(entries)


def test_every_pip_package_is_pinned_exactly() -> None:
    assert not floating_pip_packages(DOCKERFILE.read_text())


def test_the_cli_is_pinned_to_the_extension_release() -> None:
    m = re.search(r"@anthropic-ai/claude-code@(\S+)", DOCKERFILE.read_text())
    assert m, "npm install of @anthropic-ai/claude-code carries no version"
    cli = m.group(1)
    extension = next(
        e
        for e in load_defaults(ROLE)["code_server_k8s_extensions"]
        if e["name"] == "Anthropic.claude-code"
    )
    assert cli == str(extension["version"]), (
        f"CLI pinned to {cli} but the Anthropic.claude-code extension to {extension['version']} — "
        "the extension drives the CLI, so the two move together"
    )


def test_node_is_pinned_with_a_checksum() -> None:
    defaults = load_defaults(ROLE)
    version = str(defaults["code_server_k8s_node_version"])
    assert _RELEASE.match(version), f"node version {version!r} is not an exact release"
    assert _SHA256.match(str(defaults["code_server_k8s_node_sha256"]))
    rendered = _rendered_dockerfile()
    assert f"node-v{version}-linux-x64.tar.xz" in rendered
    assert "sha256sum -c" in rendered


def test_no_build_step_resolves_a_version_from_the_network() -> None:
    rendered = _rendered_dockerfile()
    back = [form for form in FLOATING_FORMS if form in rendered]
    assert not back, f"network-resolved build step(s) are back: {back}"


def test_the_rendered_build_verifies_every_extension() -> None:
    rendered = _rendered_dockerfile()
    for e in load_defaults(ROLE)["code_server_k8s_extensions"]:
        assert e["url"] in rendered, (
            f"{e['name']}: pinned URL not in the rendered Dockerfile"
        )
        assert f"{e['sha256']}  /opt/vsix/{e['name']}.vsix" in rendered, (
            f"{e['name']}: no sha256sum check in the rendered Dockerfile"
        )


# ── red proofs for the two pure checks ─────────────────────────────────────────────────────


def _entry(**overrides) -> dict:
    base = {
        "name": "charliermarsh.ruff",
        "version": "2026.82.0",
        "url": "https://open-vsx.org/api/charliermarsh/ruff/linux-x64/2026.82.0/file/x.vsix",
        "sha256": "b" * 64,
    }
    return {**base, **overrides}


def test_a_complete_extension_entry_is_clean() -> None:
    assert extension_pin_problems([_entry()]) == []


@pytest.mark.parametrize(
    "broken",
    [
        {"version": "latest"},
        {"sha256": "abc"},
        {
            "url": "https://open-vsx.org/api/charliermarsh/ruff/linux-x64/2026.81.0/file/x.vsix"
        },
        {
            "url": "https://open-vsx.org/api/charliermarsh/ruff/alpine-arm64/2026.82.0/file/x.vsix"
        },
    ],
    ids=["floating-version", "short-hash", "url-names-other-version", "wrong-platform"],
)
def test_a_broken_extension_entry_is_flagged(broken: dict) -> None:
    assert extension_pin_problems([_entry(**broken)])


def test_a_fully_pinned_pip_block_is_clean() -> None:
    text = "RUN pip install --no-cache-dir \\\n        uv==1.0.0 \\\n        ruff==0.1.0 \\\n    && true\n"
    assert floating_pip_packages(text) == []


def test_a_bare_pip_package_is_flagged() -> None:
    text = "RUN pip install --no-cache-dir \\\n        uv==1.0.0 \\\n        ruff \\\n    && true\n"
    assert floating_pip_packages(text) == ["ruff"]
