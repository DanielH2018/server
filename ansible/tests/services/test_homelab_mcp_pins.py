"""Guard: homelab-mcp's pip deps are exact, and mcp stays on the line Renovate is capped to.

The Dockerfile pins each pip dependency exactly behind a digest-pinned base, so a rebuild
resolves nothing. The pins are read off the RENDERED Dockerfile, the text k8s/image-builder
stages for the build (#3175). mcp must stay below 2: mcp 2.0.0 removes `mcp.server.fastmcp`, which app.py
imports, and the container would crash-loop. renovate.json's `allowedVersions: "<2"` rule on
`mcp` holds that line for Renovate. The two must agree: a pin that crossed the cap by hand, or
a cap that went missing, each recreates the crash-loop one way or the other.

Run: uv run pytest ansible/tests/services/test_homelab_mcp_pins.py
"""

import json
import re

from _helpers import REPO
from _k8s_render import rendered_build_text

RENOVATE = REPO / "renovate.json"

# The deps the pip line must carry, so a rewritten line fails as a missing member rather than
# passing over nothing.
KNOWN_DEPS = frozenset({"mcp", "httpx", "uvicorn"})


def pip_pins(dockerfile: str) -> dict[str, str | None]:
    """Each package on the `pip install` line mapped to its `==` version, None when bare."""
    m = re.search(r"pip install\s+(.*)", dockerfile)
    assert m, "no pip install line"
    pins: dict[str, str | None] = {}
    for token in m.group(1).split():
        if token.startswith("-"):
            continue
        name, sep, version = token.strip("'\"").partition("==")
        pins[name] = version if sep else None
    return pins


def _mcp_cap() -> str | None:
    rules = json.loads(RENOVATE.read_text())["packageRules"]
    for rule in rules:
        if rule.get("matchPackageNames") == ["mcp"]:
            return rule.get("allowedVersions")
    return None


def test_every_pip_package_is_pinned_exactly() -> None:
    pins = pip_pins(rendered_build_text("homelab-mcp"))
    missing = KNOWN_DEPS - set(pins)
    assert not missing, f"pip line lost {sorted(missing)}"
    bare = sorted(name for name, version in pins.items() if version is None)
    assert not bare, f"unpinned pip package(s): {bare}"
    ranged = sorted(
        name
        for name, version in pins.items()
        if not re.match(r"^\d+\.\d+\.\d+$", version or "")
    )
    assert not ranged, f"pip package(s) not pinned to an exact release: {ranged}"


def test_mcp_pin_is_on_the_line_renovate_is_capped_to() -> None:
    version = pip_pins(rendered_build_text("homelab-mcp"))["mcp"]
    assert version, "mcp is not pinned with =="
    assert version.split(".")[0] == "1", (
        f"mcp=={version} crosses the 2.x line that removed mcp.server.fastmcp — port app.py first"
    )
    assert _mcp_cap() == "<2", (
        "renovate.json must cap mcp with allowedVersions '<2', or Renovate offers the 2.x bump"
    )


def test_a_bare_package_reads_as_unpinned() -> None:
    assert pip_pins("RUN pip install --no-cache-dir 'mcp<2' httpx uvicorn==0.1.0") == {
        "mcp<2": None,
        "httpx": None,
        "uvicorn": "0.1.0",
    }
