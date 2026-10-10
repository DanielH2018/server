#!/usr/bin/env python3
"""Guard the k8s-images manager's `autoReplaceStringTemplate` against the two ways it corrupts.

Renovate writes a bump by replacing the WHOLE span its matchString matched. With no template it
substitutes currentValue -> newValue inside that span, which is why `pinDigests: true` refreshed
every pin that already carried an @sha256 suffix and added one to no tag-only pin. The
template is what appends a digest — and because the span starts at `_image:` rather than at the
value, a template missing that literal prefix rewrites every pin the manager touches.

The manager coverage guards are in `test_renovate_managers.py`; the Dockerfile and lockstep
guards are in `test_renovate_dockerfiles.py`.

Run: uv run pytest scripts/tests/test_renovate_auto_replace.py
"""

import re

from _renovate import (
    _RENOVATE_CONFIG,
    _REPO,
    _file_pattern_to_regex,
    _k8s_image_manager,
    _to_python_regex,
    render_auto_replace,
)

# The digest pin is named rather than counted, so a rename breaks these guards loudly instead of
# leaving them asserting over an empty set. It is a k8s role default rather than the one in
# roles/setup/k3s, which several sessions edit at once.
TEMPLATE_DIGEST_PIN = "homepage_k8s_image"

# A bare pin is the shape the template exists for: without one, Renovate substitutes
# currentValue -> newValue inside the matched text and there is no digest to substitute, so
# `pinDigests: true` never added an @sha256 suffix to a tag-only pin. The template converts every
# live bare pin it reaches, so the guards derive one from the digest pin rather than naming a
# live one: bazarr, the last named bare pin, gained its digest in Renovate #4149.
_DIGEST_SUFFIX = re.compile(r"@sha256:[0-9a-f]{64}")


def _live_k8s_image_spans(tracked: list[str]) -> list[tuple[str, str, re.Match[str]]]:
    """Every span the k8s-images manager matches across the repo, as (path, line, match)."""
    mgr = _k8s_image_manager()
    file_res = [_file_pattern_to_regex(fp) for fp in mgr["managerFilePatterns"]]
    match_res = [re.compile(_to_python_regex(ms)) for ms in mgr["matchStrings"]]
    spans = []
    for path in tracked:
        if not any(r.search(path) for r in file_res):
            continue
        for line in (_REPO / path).read_text().splitlines():
            for r in match_res:
                for m in r.finditer(line):
                    spans.append((path, line, m))
    return spans


def _bare_span(tracked: list[str]) -> tuple[str, str, re.Match[str]]:
    """TEMPLATE_DIGEST_PIN's live line with its digest removed, matched by the real manager."""
    path, line = next(
        (path, line)
        for path, line, _ in _live_k8s_image_spans(tracked)
        if line.split(":", 1)[0].strip() == TEMPLATE_DIGEST_PIN
    )
    bare_line = _DIGEST_SUFFIX.sub("", line, count=1)
    assert bare_line != line, (
        f"{TEMPLATE_DIGEST_PIN} carries no @sha256 digest to remove"
    )
    mgr = _k8s_image_manager()
    match = next(
        m
        for ms in mgr["matchStrings"]
        if (m := re.compile(_to_python_regex(ms)).search(bare_line))
    )
    assert match.group("currentDigest") is None, match.group(0)
    return path, bare_line, match


def test_the_k8s_image_template_round_trips_every_live_pin(tracked: list[str]) -> None:
    """The template must reproduce a matched span byte for byte when nothing has changed.

    Renovate replaces the WHOLE matched span with the rendered template, and the span starts at
    `_image:` rather than at the value — so a template missing that literal prefix eats the tail
    of the variable name and corrupts every pin this manager touches. The matchString also
    tolerates quotes and any run of whitespace, while the template emits one space and no
    quotes; a pin added later in either of those shapes would be silently rewritten. Rendering
    the template with newValue == currentValue and newDigest == currentDigest is the identity
    case, so this asserts an invariant rather than a copy of the config.
    """
    spans = _live_k8s_image_spans(tracked)
    by_name = {line.split(":", 1)[0].strip(): m for _, line, m in spans}
    assert (
        TEMPLATE_DIGEST_PIN in by_name
        and by_name[TEMPLATE_DIGEST_PIN].group("currentDigest") is not None
    ), (
        f"{TEMPLATE_DIGEST_PIN} no longer matches the k8s-images manager as a digest-carrying "
        "pin — the half of this guard that exercises the #if branch has nothing to run on."
    )

    template = _k8s_image_manager()["autoReplaceStringTemplate"]
    corrupted = []
    for path, line, m in [*spans, _bare_span(tracked)]:
        rendered = render_auto_replace(
            template,
            depName=m.group("depName"),
            newValue=m.group("currentValue"),
            newDigest=m.group("currentDigest"),
        )
        if rendered != m.group(0):
            corrupted.append(f"{path}: {line.strip()}\n    -> {rendered}")
    assert not corrupted, (
        "The k8s-images autoReplaceStringTemplate does not reproduce these pins unchanged, so "
        "Renovate would rewrite them into the second form on its next bump:\n"
        + "\n".join(corrupted)
    )

    # Proof this guard can go RED: the same template without its literal `_image: ` prefix
    # corrupts the very first span it touches.
    path, line, m = spans[0]
    assert render_auto_replace(
        template.replace("_image: ", "", 1),
        depName=m.group("depName"),
        newValue=m.group("currentValue"),
        newDigest=m.group("currentDigest"),
    ) != m.group(0), (
        f"a prefix-less template round-tripped {line.strip()!r} — this guard proves nothing"
    )


def test_the_k8s_image_template_pins_a_digest_onto_a_bare_tag(
    tracked: list[str],
) -> None:
    """The behaviour the template exists for: a tag-only pin gains an @sha256 suffix.

    `pinDigests: true` alone adds a digest to nothing, because the templateless rewrite can only
    substitute a digest that is already there. The `#if newDigest` block is what appends one,
    and it must leave an unpinnable update (no newDigest) alone.
    """
    template = _k8s_image_manager()["autoReplaceStringTemplate"]
    _, _, bare = _bare_span(tracked)

    digest = "sha256:" + "ab" * 32
    pinned = render_auto_replace(
        template,
        depName=bare.group("depName"),
        newValue=bare.group("currentValue"),
        newDigest=digest,
    )
    assert (
        pinned
        == f"_image: {bare.group('depName')}:{bare.group('currentValue')}@{digest}"
    )

    unpinned = render_auto_replace(
        template,
        depName=bare.group("depName"),
        newValue=bare.group("currentValue"),
        newDigest=None,
    )
    assert unpinned == bare.group(0)


def test_a_pin_digest_automerges_unless_the_role_is_denied_auto_deploy():
    """Renovate types the update this template makes possible as `pinDigest`, not `digest`.

    A rule matching only `digest` automerged none of the tag-only pins, so each opened a
    hand-merge PR. The denylist rule must stay unscoped by update type, or a denied
    role's pinDigest would fall through to the automerge rule above it.
    """
    rules = _RENOVATE_CONFIG["packageRules"]
    assert any(
        r.get("automerge") and "pinDigest" in r.get("matchUpdateTypes", [])
        for r in rules
    ), "no automerge rule matches updateType pinDigest"
    denylist = [
        r
        for r in rules
        if str(r.get("groupName", "")).startswith("k8s image {{depName}} (manual")
    ]
    assert len(denylist) == 1, denylist
    assert "matchUpdateTypes" not in denylist[0], denylist[0]["matchUpdateTypes"]
