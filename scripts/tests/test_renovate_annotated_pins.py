"""Guards on the `# renovate:` annotated pins.

A pinned artifact is declared to Renovate by one annotation above its `_version:` line
(lib/renovate_annotations.py). CI's extract check (`scripts/validate/renovate_extract.py`)
fails an annotation Renovate does not read and a version a pinned download renders from that
nothing tracks. What it cannot see stays here: an extractVersion that strips the `v` from a
pin carrying one, so Renovate writes back a bare version and every URL built from the pin 404s.

The packageRule that finishes an annotated pin, and the per-package rules after it that it
yields to, are the `annotated-`, `crowdsec-` and `node-exporter-` rows of
test_renovate_package_rules.py.

Run: uv run pytest scripts/tests/test_renovate_annotated_pins.py
"""

import re
from collections.abc import Callable

from _renovate import EXPECTED_DEPS, _CONFIG_MANAGERS, _REPO
from lib.renovate_annotations import (
    annotations_in,
    is_annotation_manager,
    to_python_regex,
)

ANNOTATION_MANAGER = next(m for m in _CONFIG_MANAGERS if is_annotation_manager(m))


def _read(rel: str) -> str:
    return (_REPO / rel).read_text(errors="replace")


def _annotated_pins() -> list[dict[str, str]]:
    """Each annotation in a file Renovate extracts from, with `file` added."""
    files = sorted({d.file for d in EXPECTED_DEPS if d.manager == "regex"})
    return [
        {**groups, "file": path}
        for path in files
        for groups in annotations_in(ANNOTATION_MANAGER, _read(path))
    ]


def tag_rewriting_pins(pins: list[dict], read: Callable[[str], str]) -> list[str]:
    """`<file>: <variable>` for each `v`-prefixed annotated pin its own extractVersion rewrites.

    Renovate writes the extracted version back into the pin, so an extractVersion that strips
    the `v` turns `v0.16.0` into `0.16.2`, and a URL that renders the pin as a git ref 404s.
    """
    problems = []
    for pin in pins:
        if "extractVersion" not in pin:
            continue
        path, variable = pin["file"], pin["variable"]
        line = re.search(rf'(?m)^\s*{re.escape(variable)}:\s*"?([^"\s]+)"?', read(path))
        current = line.group(1) if line else ""
        if not current.startswith("v"):
            continue
        extracted = re.search(to_python_regex(pin["extractVersion"]), current)
        if extracted is None or extracted["version"] != current:
            problems.append(f"{path}: {variable}")
    return problems


def _annotated(extract: str, value: str = "v1.2.0") -> tuple[list[dict], Callable]:
    pins = [
        {
            "file": "roles/x/defaults/main.yml",
            "variable": "x_version",
            "extractVersion": extract,
        }
    ]
    return pins, lambda _: f"x_version: {value}\n"


def test_every_v_prefixed_annotated_pin_keeps_its_v():
    pins = _annotated_pins()
    assert "k3s_metallb_version" in {
        p["variable"] for p in pins if "extractVersion" in p
    }
    problems = tag_rewriting_pins(pins, _read)
    assert not problems, (
        "an extractVersion strips the `v` from a pin that carries one, so Renovate writes the "
        "bare version back and every URL built from the pin breaks. Capture the `v` inside "
        "the version group:\n" + "\n".join(problems)
    )


def test_an_extract_version_keeping_the_v_is_clean():
    assert tag_rewriting_pins(*_annotated(r"^(?<version>v\d+\.\d+\.\d+)$")) == []


def test_an_extract_version_stripping_the_v_is_flagged():
    assert tag_rewriting_pins(*_annotated(r"^v(?<version>.+)$")) == [
        "roles/x/defaults/main.yml: x_version"
    ]


def test_the_metallb_anchor_drops_the_chart_releases():
    metallb = next(
        p for p in _annotated_pins() if p["variable"] == "k3s_metallb_version"
    )
    anchor = re.compile(to_python_regex(metallb["extractVersion"]))
    assert anchor.search("v0.16.2")
    assert not anchor.search("metallb-chart-0.16.1")
