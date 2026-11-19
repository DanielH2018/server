"""Guards on the `# renovate:` annotated pins and the one packageRule that finishes them.

A pinned artifact is declared to Renovate by one annotation above its `_version:` line
(lib/renovate_annotations.py). CI's extract check (`scripts/validate/renovate_extract.py`)
fails an annotation Renovate does not read and a version a pinned download renders from that
nothing tracks. Three things it cannot see stay here:

- An extractVersion that strips the `v` from a pin carrying one, so Renovate writes back a
  bare version and every URL built from the pin 404s.
- The annotated-pin packageRule sitting after a per-package rule it should yield to, which
  strips `k8s_autodeploy: false` from the crowdsec bouncer plugin's title so the unattended
  agent lands a traefik redeploy.
- The Pi's node_exporter pin (a `homelab-tools` tool since H7.25) and the cluster image
  splitting into two PRs. A test holds the two equal, so each PR would stay red until the
  other merged.

Run: uv run pytest scripts/tests/test_renovate_annotated_pins.py
"""

import re
from collections.abc import Callable

from _renovate import (
    EXPECTED_DEPS,
    _CONFIG_MANAGERS,
    _PACKAGE_RULES,
    _REPO,
    _resolve_group_name,
    _resolve_setting,
)
from lib.renovate_annotations import (
    annotations_in,
    is_annotation_manager,
    to_python_regex,
)

ANNOTATION_MANAGER = next(m for m in _CONFIG_MANAGERS if is_annotation_manager(m))
DEP_TYPE = ANNOTATION_MANAGER["depTypeTemplate"]

TRAEFIK_DEFAULTS = "k8s/roles/traefik/defaults/main.yml"
PI_TOOL_PINS = "hosts/packages/homelab-tools/package.yml"
NODE_EXPORTER_DEFAULTS = "k8s/roles/node-exporter/defaults/main.yml"
CROWDSEC_PLUGIN = "maxlerebourg/crowdsec-bouncer-traefik-plugin"
DENYLIST_MARKER = "k8s_autodeploy: false"


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


# ── the packageRule that finishes an annotated pin ────────────────────────────────────────


def test_an_annotated_pin_is_a_manual_pr_naming_the_refresh_command():
    # A dep no per-package rule names, at a path the annotation manager reads, so only the
    # depType rule can decide it.
    path = "hosts/components/k3s-server/component.yml"
    group = _resolve_group_name(
        "vendor/tool",
        path,
        "minor",
        "github-releases",
        dep_type=DEP_TYPE,
    )
    assert group is not None and "asset_pins.py --refresh {{depName}}" in group, group
    automerge = _resolve_setting(
        "automerge",
        "vendor/tool",
        path,
        "minor",
        "github-releases",
        dep_type=DEP_TYPE,
    )
    assert automerge is False, (
        "an annotated pin's paired digest is stale on every bump, so it must never automerge"
    )


def test_a_later_per_package_rule_keeps_the_crowdsec_denylist_marker():
    group = _resolve_group_name(
        CROWDSEC_PLUGIN, TRAEFIK_DEFAULTS, "minor", "github-releases", dep_type=DEP_TYPE
    )
    assert group is not None and DENYLIST_MARKER in group, group


def test_the_annotated_pin_rule_moved_last_would_strip_the_marker():
    """The red half: the same resolution with the annotated-pin rule moved to the end."""
    at = next(
        i for i, r in enumerate(_PACKAGE_RULES) if r.get("matchDepTypes") == [DEP_TYPE]
    )
    reordered = _PACKAGE_RULES[:at] + _PACKAGE_RULES[at + 1 :] + [_PACKAGE_RULES[at]]
    group = _resolve_group_name(
        CROWDSEC_PLUGIN,
        TRAEFIK_DEFAULTS,
        "minor",
        "github-releases",
        rules=reordered,
        dep_type=DEP_TYPE,
    )
    assert group is not None and DENYLIST_MARKER not in group, group


# ── the two node_exporter pins move in one PR ─────────────────────────────────────────────

# The cluster image and the Pi's `homelab-tools` pin, which
# hosts/tests/test_pi_node_exporter_host_unit.py holds equal.
NODE_EXPORTER_TWINS = (
    ("prom/node-exporter", NODE_EXPORTER_DEFAULTS, "docker", None),
    ("prometheus/node_exporter", PI_TOOL_PINS, "github-release-attachments", None),
)


def _twin_groups(rules: list[dict] = _PACKAGE_RULES) -> set[str | None]:
    return {
        _resolve_group_name(dep, path, "minor", ds, rules=rules, dep_type=dt)
        for dep, path, ds, dt in NODE_EXPORTER_TWINS
    }


def test_the_node_exporter_twins_share_one_manual_group():
    groups = _twin_groups()
    assert len(groups) == 1 and None not in groups, (
        f"the two node_exporter pins resolve to {groups}; split PRs each go red on the "
        "equality test until the other merges"
    )
    assert "homelab-tools node-exporter pin" in str(groups.pop())
    for dep, path, ds, dt in NODE_EXPORTER_TWINS:
        automerge = _resolve_setting("automerge", dep, path, "minor", ds, dep_type=dt)
        assert automerge is False, f"{dep} automerges a version bump: {automerge}"


def test_the_twin_rule_moved_before_the_homelab_tools_rule_splits_them():
    """The red half: the homelab-tools rule then overrides the Pi pin's groupName."""
    at = next(
        i
        for i, r in enumerate(_PACKAGE_RULES)
        if PI_TOOL_PINS in r.get("matchFileNames", [])
    )
    twin = next(
        i
        for i, r in enumerate(_PACKAGE_RULES)
        if "prometheus/node_exporter" in r.get("matchPackageNames", [])
    )
    rules = [r for i, r in enumerate(_PACKAGE_RULES) if i != twin]
    rules.insert(at, _PACKAGE_RULES[twin])
    assert len(_twin_groups(rules)) == 2


def test_a_node_exporter_image_digest_still_automerges():
    automerge = _resolve_setting(
        "automerge", "prom/node-exporter", NODE_EXPORTER_DEFAULTS, "digest", "docker"
    )
    assert automerge is True, (
        "a digest re-push moves no version, so the Pi has nothing to follow; the lockstep "
        "rule must leave it to the k8s digest automerge"
    )
