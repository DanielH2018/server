#!/usr/bin/env python3
"""Guard the k8s-images manager's `autoReplaceStringTemplate` against the two ways it corrupts.

Renovate writes a bump by replacing the WHOLE span its matchString matched. With no template it
substitutes currentValue -> newValue inside that span, which is why `pinDigests: true` refreshed
every pin that already carried an @sha256 suffix and added one to no tag-only pin (#2392). The
template is what appends a digest — and because the span starts at `_image:` rather than at the
value, a template missing that literal prefix rewrites every pin the manager touches.

The manager coverage guards are in `test_renovate_managers.py`; the Dockerfile and lockstep
guards are in `test_renovate_dockerfiles.py`.

Run: uv run pytest scripts/tests/test_renovate_auto_replace.py
"""

import re

from _renovate import (
    _REPO,
    _file_pattern_to_regex,
    _k8s_image_manager,
    _to_python_regex,
    render_auto_replace,
)

# A bare pin is the shape the template exists for (#2392): without one, Renovate substitutes
# currentValue -> newValue inside the matched text and there is no digest to substitute, so
# `pinDigests: true` never added an @sha256 suffix to a tag-only pin. Both named pins below are
# the ones that issue's verify-by step names, so a rename breaks these guards loudly rather
# than leaving them asserting over an empty set.
TEMPLATE_BARE_PIN = "bazarr_k8s_image"
TEMPLATE_DIGEST_PIN = "k3s_longhorn_restore_drill_image"


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
    names = {line.split(":", 1)[0].strip() for _, line, _ in spans}
    assert TEMPLATE_BARE_PIN in names, (
        f"{TEMPLATE_BARE_PIN} no longer matches the k8s-images manager — this guard is "
        "asserting over a set that no longer contains the pin shape it was written for."
    )
    assert TEMPLATE_DIGEST_PIN in names, (
        f"{TEMPLATE_DIGEST_PIN} no longer matches the k8s-images manager — the digest-carrying "
        "half of this guard has nothing to exercise."
    )

    template = _k8s_image_manager()["autoReplaceStringTemplate"]
    corrupted = []
    for path, line, m in spans:
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

    # Proof this guard can go RED: the same template without its literal `_image: ` prefix is
    # the form #2392's reviewer first proposed, and it corrupts the very first span it touches.
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
    """The behaviour #2392 exists for: a tag-only pin gains an @sha256 suffix.

    `pinDigests: true` has been on since 5ba111de8 and added a digest to nothing, because the
    templateless rewrite can only substitute a digest that is already there. The `#if newDigest`
    block is what appends one, and it must leave an unpinnable update (no newDigest) alone.
    """
    template = _k8s_image_manager()["autoReplaceStringTemplate"]
    bare = next(
        (
            m
            for _, line, m in _live_k8s_image_spans(tracked)
            if line.split(":", 1)[0].strip() == TEMPLATE_BARE_PIN
        ),
        None,
    )
    assert bare is not None, f"{TEMPLATE_BARE_PIN} matched no span"
    assert bare.group("currentDigest") is None, (
        f"{TEMPLATE_BARE_PIN} now carries a digest — pick another tag-only pin for this guard."
    )

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
