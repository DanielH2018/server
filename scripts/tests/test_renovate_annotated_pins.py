"""Guards on the `# renovate:` annotated pins and the one packageRule that finishes them.

A pinned artifact is declared to Renovate by one annotation above its `_version:` line
(lib/renovate_annotations.py). Three things can still let one age silently or land unfinished:

- An annotation the manager's regex does not read: keys out of order, or a blank line before
  the pin. Renovate reports nothing; the pin is simply untracked.
- A pin whose URL renders from a version nothing tracks. `asset_pins.py` fetches it, but no
  PR ever bumps it.
- The annotated-pin packageRule sitting after a per-package rule it should yield to, which
  strips `k8s_autodeploy: false` from the crowdsec bouncer plugin's title so the unattended
  agent lands a traefik redeploy.

Run: uv run pytest scripts/tests/test_renovate_annotated_pins.py
"""

import re
from collections.abc import Callable, Iterable

from _renovate import (
    _CONFIG_MANAGERS,
    _MANAGERS,
    _PACKAGE_RULES,
    _REPO,
    _file_pattern_to_regex,
    _resolve_group_name,
    _resolve_setting,
    _to_python_regex,
)
from lib.renovate_annotations import annotations_in, is_annotation_manager
from validate.asset_pins import Pin, discover_pins

ANNOTATION_MANAGER = next(m for m in _CONFIG_MANAGERS if is_annotation_manager(m))
DEP_TYPE = ANNOTATION_MANAGER["depTypeTemplate"]

# depNames the expansion must find, one per datasource and plane, so a regex that stops
# matching fails by name instead of shrinking the census to nothing.
KNOWN_ANNOTATED = frozenset(
    {
        "getsops/sops",
        "k3s-io/k3s",
        "coredns/coredns",
        "docker-ce",
        "maxlerebourg/crowdsec-bouncer-traefik-plugin",
        "node",
        "prometheus/node_exporter",
    }
)

# Version keys a pin's URL renders from that no manager tracks, on purpose.
UNTRACKED_BY_DECISION: dict[str, str] = {}

_ANNOTATION_LINE = re.compile(r"^\s*#\s*renovate:", re.MULTILINE)

SOPS_DEFAULTS = "ansible/roles/setup/sops_setup/defaults/main.yml"
TRAEFIK_DEFAULTS = "ansible/roles/k8s/traefik/defaults/main.yml"
PI_DEFAULTS = "ansible/roles/setup/optimize_pi/defaults/main.yml"
NODE_EXPORTER_DEFAULTS = "ansible/roles/k8s/node-exporter/defaults/main.yml"
CROWDSEC_PLUGIN = "maxlerebourg/crowdsec-bouncer-traefik-plugin"
DENYLIST_MARKER = "k8s_autodeploy: false"


def _read(rel: str) -> str:
    return (_REPO / rel).read_text(errors="replace")


def unread_annotations(text: str, manager: dict = ANNOTATION_MANAGER) -> int:
    """How many `# renovate:` comment lines in `text` the annotation manager does not read."""
    return len(_ANNOTATION_LINE.findall(text)) - len(annotations_in(manager, text))


def untracked_pin_versions(
    pins: Iterable[Pin],
    managers: list[dict],
    read: Callable[[str], str],
    exempt: Iterable[str] = (),
) -> list[str]:
    """`<file>: <key> (pin <name>)` for each `_version` key a pin renders from that no
    manager's matchString covers in that file. Empty means every one is tracked."""
    exempt = set(exempt)
    problems = []
    for pin in pins:
        for key in sorted(pin.inputs):
            if not key.endswith("_version") or key in exempt:
                continue
            text = read(pin.source)
            owns_line = re.compile(rf"(?m)^\s*{re.escape(key)}:")
            tracked = any(
                owns_line.search(match.group(0))
                for m in managers
                if any(
                    _file_pattern_to_regex(p).search(pin.source)
                    for p in m["managerFilePatterns"]
                )
                for ms in m["matchStrings"]
                for match in re.finditer(_to_python_regex(ms), text)
            )
            if not tracked:
                problems.append(f"{pin.source}: {key} (pin {pin.name})")
    return problems


# ── the annotations themselves ────────────────────────────────────────────────────────────


def test_the_expansion_finds_the_known_annotated_pins():
    found = {m["depNameTemplate"] for m in _MANAGERS if "annotation" in m}
    missing = KNOWN_ANNOTATED - found
    assert not missing, f"no `# renovate:` annotation expands to {sorted(missing)}"


def test_every_annotation_comment_in_role_defaults_is_read():
    patterns = [
        _file_pattern_to_regex(p) for p in ANNOTATION_MANAGER["managerFilePatterns"]
    ]
    defaults = sorted(
        str(p.relative_to(_REPO))
        for p in (_REPO / "ansible/roles").glob("*/*/defaults/main.yml")
    )
    scanned = [rel for rel in defaults if any(p.search(rel) for p in patterns)]
    assert SOPS_DEFAULTS in scanned and TRAEFIK_DEFAULTS in scanned
    unread = [rel for rel in scanned if unread_annotations(_read(rel))]
    assert not unread, (
        "a `# renovate:` comment the annotation manager does not read: its keys must be "
        "`datasource= depName= [versioning=] [extractVersion=] [registryUrl=]` in that order, "
        f"directly above a `<key>_version:` line. In: {unread}"
    )


def test_a_well_formed_annotation_is_read():
    text = '# renovate: datasource=github-releases depName=a/b\nb_version: "1.0"\n'
    assert unread_annotations(text) == 0


def test_an_annotation_with_keys_out_of_order_is_unread():
    text = '# renovate: depName=a/b datasource=github-releases\nb_version: "1.0"\n'
    assert unread_annotations(text) == 1


# ── every version a pinned download renders from is tracked ───────────────────────────────


def test_every_version_a_pin_renders_from_is_tracked():
    pins, _ = discover_pins()
    with_versions = [p for p in pins if any(k.endswith("_version") for k in p.inputs)]
    assert {
        "sops_setup_binary_amd64",
        "k3s_install_script",
        "optimize_pi_node_exporter",
    } <= {p.name for p in with_versions}
    problems = untracked_pin_versions(
        with_versions, _MANAGERS, _read, UNTRACKED_BY_DECISION
    )
    assert not problems, (
        "a pinned download renders from a version no Renovate manager tracks, so it is "
        "fetched and checked but never bumped. Add `# renovate: datasource=... depName=...` "
        "above the version line:\n" + "\n".join(problems)
    )


def _fake_pin(source: str = "roles/x/defaults/main.yml") -> Pin:
    return Pin(
        "tool",
        "https://host/tool",
        "sha256",
        "0" * 64,
        source,
        inputs=frozenset({"tool_version"}),
    )


def test_a_pin_version_a_manager_matches_is_clean():
    managers = [
        {
            "managerFilePatterns": ["/^roles/x/defaults/main\\.yml$/"],
            "matchStrings": ['tool_version:\\s*"(?<currentValue>[^"]+)"'],
        }
    ]
    read = lambda _: 'tool_version: "1.0"\n'
    assert untracked_pin_versions([_fake_pin()], managers, read) == []


def test_a_pin_version_no_manager_matches_is_flagged():
    managers = [
        {
            "managerFilePatterns": ["/^roles/x/defaults/main\\.yml$/"],
            "matchStrings": ['other_version:\\s*"(?<currentValue>[^"]+)"'],
        }
    ]
    read = lambda _: 'tool_version: "1.0"\nother_version: "2.0"\n'
    assert untracked_pin_versions([_fake_pin()], managers, read) == [
        "roles/x/defaults/main.yml: tool_version (pin tool)"
    ]


# ── the packageRule that finishes an annotated pin ────────────────────────────────────────


def test_an_annotated_pin_is_a_manual_pr_naming_the_refresh_command():
    group = _resolve_group_name(
        "getsops/sops", SOPS_DEFAULTS, "minor", "github-releases", dep_type=DEP_TYPE
    )
    assert group is not None and "asset_pins.py --refresh {{depName}}" in group, group
    automerge = _resolve_setting(
        "automerge",
        "getsops/sops",
        SOPS_DEFAULTS,
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

# The cluster image and the Pi's annotated tarball pin, which
# ansible/tests/setup/test_pi_node_exporter_host_unit.py holds equal.
NODE_EXPORTER_TWINS = (
    ("prom/node-exporter", NODE_EXPORTER_DEFAULTS, "docker", None),
    ("prometheus/node_exporter", PI_DEFAULTS, "github-releases", DEP_TYPE),
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
    assert "asset_pins.py --refresh prometheus/node_exporter" in str(groups.pop())
    for dep, path, ds, dt in NODE_EXPORTER_TWINS:
        automerge = _resolve_setting("automerge", dep, path, "minor", ds, dep_type=dt)
        assert automerge is False, f"{dep} automerges a version bump: {automerge}"


def test_the_twin_rule_moved_before_the_annotated_pin_rule_splits_them():
    """The red half: the annotated-pin rule then overrides the Pi pin's groupName."""
    at = next(
        i for i, r in enumerate(_PACKAGE_RULES) if r.get("matchDepTypes") == [DEP_TYPE]
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
