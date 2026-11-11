"""The host test image holds every Depends of every package a host_packages case installs.

snapshot.ubuntu.com answers 5xx often enough to fail a case that fetches from it, so the
Dockerfile installs or pre-downloads each Depends. Commit 4df12bdcb made `gh` a Depends of the
`apt-github-cli` component, the list never gained it, and the repos case failed three runs on
2026-11-11 with `502 Bad Gateway` on `gh_2.46.0-4_amd64.deb`.

Run: uv run pytest ansible/tests/repo/test_host_test_image_predownloads.py
"""

import re
from pathlib import Path

from _helpers import REPO
from lib import yaml_fast

HOSTS = REPO / "hosts"
DOCKERFILE = HOSTS / "lib" / "tests" / "container" / "Dockerfile"
# What the cases install: the real Pi package, the shared packages and every fixture variant.
SPECS = (
    HOSTS / "daniel-pi" / "package.yml",
    *sorted((HOSTS / "packages").glob("*/package.yml")),
    *sorted((HOSTS / "lib" / "tests" / "container" / "fixture").glob("*.yml")),
)
# The renderer adds this to every host package (renderer_lib/host.py:HOST_DEPENDS).
RENDERER_DEPENDS = ("python3",)
# Each case builds the homelab-* packages itself and serves them from its own repository.
BUILT = re.compile(r"homelab-")
APT_ARGS = re.compile(r"apt-get\b[^;&]*?\binstall\b([^;&]*)")
STAND_INS = re.compile(r"for package in ([^;]+);")


def _name(entry: str) -> str:
    return re.split(r"[\s(]", entry.strip(), maxsplit=1)[0]


def case_depends(specs=SPECS) -> dict[str, str]:
    """Each Depends a case installs, mapped to the spec that brought it in."""
    found = {name: "the renderer" for name in RENDERER_DEPENDS}
    for spec in specs:
        data = yaml_fast.safe_load(spec.read_text(encoding="utf-8")) or {}
        where = str(spec.relative_to(REPO) if spec.is_relative_to(REPO) else spec)
        for entry in data.get("depends") or []:
            found.setdefault(_name(str(entry)), where)
        for component in data.get("components") or []:
            path = HOSTS / "components" / component / "component.yml"
            unit = yaml_fast.safe_load(path.read_text(encoding="utf-8")) or {}
            for entry in unit.get("depends") or []:
                found.setdefault(_name(str(entry)), f"{where} via {component}")
    return found


def image_packages(dockerfile: str) -> set[str]:
    """Every package the Dockerfile installs, pre-downloads or stands in for."""
    code = (
        line for line in dockerfile.splitlines() if not line.lstrip().startswith("#")
    )
    text = "\n".join(code).replace("\\\n", " ")
    held = set()
    for match in APT_ARGS.finditer(text):
        held.update(t for t in match[1].split() if not t.startswith(("-", "/", '"')))
    for match in STAND_INS.finditer(text):
        held.update(match[1].split())
    return held


def missing(depends: dict[str, str], dockerfile: str) -> list[str]:
    held = image_packages(dockerfile)
    return sorted(
        f"{name} ({where})"
        for name, where in depends.items()
        if name not in held and not BUILT.match(name)
    )


def _dockerfile() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


def test_the_image_holds_every_case_depends_is_clean() -> None:
    assert missing(case_depends(), _dockerfile()) == []


def test_the_census_finds_the_depends_it_must() -> None:
    depends = case_depends()
    assert {"gh", "tree", "zram-tools", "python3"} <= depends.keys(), sorted(depends)
    assert {"gh", "tree", "age", "linux-raspi"} <= image_packages(_dockerfile())


def test_a_depends_the_image_lacks_is_flagged(tmp_path: Path) -> None:
    spec = tmp_path / "package.yml"
    spec.write_text("depends: [tree, cowsay (>= 3)]\ncomponents: [apt-github-cli]\n")
    without_gh = re.sub(r"\bgh\b", "", _dockerfile())
    assert missing(case_depends((spec,)), without_gh) == [
        f"cowsay ({spec})",
        f"gh ({spec} via apt-github-cli)",
    ]
