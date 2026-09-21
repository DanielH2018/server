"""Guard: homelab-mcp's pip deps are exact, and mcp stays on the line Renovate is capped to.

The Dockerfile held `pip install 'mcp<2' httpx uvicorn` behind a digest-pinned base — a range
plus two unbounded names, re-resolved on every rebuild (#2150). The range existed because mcp
2.0.0 removed `mcp.server.fastmcp`, which app.py imports, and crash-looped the container. The
exact pin replaces the range, and renovate.json's `allowedVersions: "<2"` rule on `mcp`
replaces what the range used to say to a resolver. The two must agree: a pin that crossed the
cap by hand, or a cap that went missing, each recreates the crash-loop one way or the other.

Run: uv run pytest ansible/tests/services/test_homelab_mcp_pins.py
"""

import json
import re

from _helpers import K8S_ROLES, REPO

DOCKERFILE = K8S_ROLES / "homelab-mcp" / "templates" / "Dockerfile.j2"
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
    pins = pip_pins(DOCKERFILE.read_text())
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
    version = pip_pins(DOCKERFILE.read_text())["mcp"]
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
