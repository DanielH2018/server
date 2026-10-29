#!/usr/bin/env python3
"""Guard the Renovate manager and rules over the Pi's Compose images (target slice H5, PR H5.11).

The manager reads `image:` lines in hosts/components/pi-compose/services/*/compose.yaml.j2.
Its matchString starts at `image:` rather than at a line start, so the guards here check
three things the generic census in `test_renovate_managers.py` cannot see:

- it matches every service's `image:` line and no other line, such as a comment;
- its autoReplaceStringTemplate writes each live pin back unchanged;
- the packageRules resolve as the operator ruled on 2026-10-29 (H5's Open question 1).
  Digest bumps to autoheal and docker-proxy automerge. wg-easy stays manual. The two alloy
  pins share one manual group.

Run: uv run pytest scripts/tests/test_renovate_pi_compose_pins.py
"""

import re

import pytest

from _renovate import (
    _MANAGERS,
    _PACKAGE_RULES,
    _REPO,
    _file_pattern_to_regex,
    _resolve_setting,
    _to_python_regex,
    render_auto_replace,
)
from test_renovate_managers import _pin_digest_rule_for

_SERVICES = "hosts/components/pi-compose/services"

# Named rather than derived, so a service that loses its manager fails as a missing member
# instead of shrinking the census.
PI_IMAGES = {
    f"{_SERVICES}/alloy/compose.yaml.j2": {"grafana/alloy"},
    f"{_SERVICES}/autoheal/compose.yaml.j2": {"willfarrell/autoheal"},
    f"{_SERVICES}/docker-proxy/compose.yaml.j2": {"lscr.io/linuxserver/socket-proxy"},
    f"{_SERVICES}/wg-easy/compose.yaml.j2": {"ghcr.io/wg-easy/wg-easy"},
}

LOKI_HOMELAB_DEFAULTS = "ansible/roles/k8s/loki-homelab/defaults/main.yml"


def _pi_manager() -> dict:
    found = [
        m for m in _MANAGERS if any("pi-compose" in p for p in m["managerFilePatterns"])
    ]
    assert len(found) == 1, f"expected one manager over pi-compose, found {len(found)}"
    return found[0]


def _match_res(mgr: dict) -> list[re.Pattern]:
    return [re.compile(_to_python_regex(ms)) for ms in mgr["matchStrings"]]


def _line_mismatches(text: str, match_res: list[re.Pattern]) -> list[str]:
    """Lines the manager matches that are not `image:` lines, and `image:` lines it misses."""
    problems = []
    for n, line in enumerate(text.splitlines(), 1):
        is_image_line = line.strip().startswith("image:")
        matched = any(r.search(line) for r in match_res)
        if matched != is_image_line:
            kind = "matched a non-image line" if matched else "missed an image line"
            problems.append(f"{n}: {kind}: {line.strip()}")
    return problems


def test_the_manager_reads_exactly_the_four_compose_files(tracked: list[str]) -> None:
    file_res = [_file_pattern_to_regex(p) for p in _pi_manager()["managerFilePatterns"]]
    matched = {f for f in tracked if any(r.search(f) for r in file_res)}
    assert matched == set(PI_IMAGES)


@pytest.mark.parametrize("path", sorted(PI_IMAGES))
def test_the_manager_matches_every_image_line_and_nothing_else(path: str) -> None:
    match_res = _match_res(_pi_manager())
    text = (_REPO / path).read_text()
    assert not _line_mismatches(text, match_res)
    dep_names = {m.group("depName") for r in match_res for m in r.finditer(text)}
    assert dep_names == PI_IMAGES[path]


def test_a_comment_carrying_an_image_reference_is_flagged() -> None:
    """Proof the line check can go red: the unanchored matchString also hits a comment."""
    text = "    image: a/b:1\n    # was image: a/b:0 before the bump\n"
    assert _line_mismatches(text, _match_res(_pi_manager())) == [
        "2: matched a non-image line: # was image: a/b:0 before the bump"
    ]


def test_the_template_round_trips_every_live_pin() -> None:
    mgr = _pi_manager()
    spans = [
        m
        for path in sorted(PI_IMAGES)
        for r in _match_res(mgr)
        for m in r.finditer((_REPO / path).read_text())
    ]
    assert len(spans) == 6, "alloy 1, autoheal 1, docker-proxy 3, wg-easy 1"
    assert all(m.group("currentDigest") for m in spans)

    def render(template: str, m: re.Match[str]) -> str:
        return render_auto_replace(
            template,
            depName=m.group("depName"),
            newValue=m.group("currentValue"),
            newDigest=m.group("currentDigest"),
        )

    template = mgr["autoReplaceStringTemplate"]
    assert [render(template, m) for m in spans] == [m.group(0) for m in spans]
    # Red proof: without the literal prefix the template eats `image: ` from the line.
    assert render(template.replace("image: ", "", 1), spans[0]) != spans[0].group(0)


def _resolve(field: str, image: str, path: str, update_type: str, rules=_PACKAGE_RULES):
    return _resolve_setting(field, image, path, update_type, "docker", rules=rules)


_AUTOHEAL = ("willfarrell/autoheal", f"{_SERVICES}/autoheal/compose.yaml.j2")
_DOCKER_PROXY = (
    "lscr.io/linuxserver/socket-proxy",
    f"{_SERVICES}/docker-proxy/compose.yaml.j2",
)
_WG_EASY = ("ghcr.io/wg-easy/wg-easy", f"{_SERVICES}/wg-easy/compose.yaml.j2")
_ALLOY = ("grafana/alloy", f"{_SERVICES}/alloy/compose.yaml.j2")


@pytest.mark.parametrize("pin", [_AUTOHEAL, _DOCKER_PROXY], ids=lambda p: p[0])
def test_a_digest_bump_to_autoheal_or_docker_proxy_automerges(pin) -> None:
    assert _resolve("automerge", *pin, "digest") is True
    assert _resolve("groupName", *pin, "digest") == "pi image {{depName}}"
    # Neither registry returns a timestamp for a `latest` digest (#3368).
    assert (
        _resolve("minimumReleaseAgeBehaviour", *pin, "digest") == "timestamp-optional"
    )
    assert _resolve("rebaseWhen", *pin, "digest") == "conflicted"


@pytest.mark.parametrize("update_type", ["digest", "major"])
def test_wg_easy_stays_manual(update_type: str) -> None:
    assert _resolve("automerge", *_WG_EASY, update_type) is False
    assert "(manual" in _resolve("groupName", *_WG_EASY, update_type)


@pytest.mark.parametrize("update_type", ["digest", "minor", "major"])
def test_the_two_alloy_pins_share_one_manual_group(update_type: str) -> None:
    pi_group = _resolve("groupName", *_ALLOY, update_type)
    cluster_group = _resolve(
        "groupName", "grafana/alloy", LOKI_HOMELAB_DEFAULTS, update_type
    )
    assert pi_group == cluster_group
    assert "(manual" in pi_group and "k8s_autodeploy: false" in pi_group
    assert _resolve("automerge", *_ALLOY, update_type) is False


def test_without_the_pi_rules_alloy_falls_into_the_automerging_catch_all() -> None:
    """Proof the resolution guards can go red: the Pi rules are what hold these pins back."""
    rules = [
        r
        for r in _PACKAGE_RULES
        if not any("pi-compose" in g for g in r.get("matchFileNames", []))
        or r.get("pinDigests")
    ]
    assert _resolve("automerge", *_ALLOY, "minor", rules=rules) is True
    assert _resolve("groupName", *_ALLOY, "minor", rules=rules) == (
        "container images (non-major)"
    )


@pytest.mark.parametrize("path", sorted(PI_IMAGES))
def test_the_pin_digests_rule_reaches_each_compose_file(path: str) -> None:
    """Without it a version bump carries no newDigest, and the Pi pulls the old digest."""
    assert _pin_digest_rule_for(path) is _PACKAGE_RULES[-1]
