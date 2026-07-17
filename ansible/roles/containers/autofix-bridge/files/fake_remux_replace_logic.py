"""Pure decision core for the fake-remux reconciler (fake_remux_replace.py).

Stdlib-only, 3.12-clean (single-exception except only), no network so it is unit-testable without
Sonarr/docker. Split from the I/O shell exactly like fake_remux_logic.py is split from
fake_remux_scan.py.
"""

from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fake_remux_logic as frl  # noqa: E402  (sibling module — the detector's signal, reused)

_WRONG_MAP = re.compile(r"wasn.?t requested", re.I)
_BLOCKED = re.compile(r"blocked till|is disabled|unavailable", re.I)


def matches_any(text, needles) -> bool:
    t = (text or "").lower()
    return any(str(n).lower() in t for n in (needles or []))


def _rejections(rel):
    return [str(r) for r in (rel.get("rejections") or [])]


def is_wrong_map(rel) -> bool:
    return any(_WRONG_MAP.search(r) for r in _rejections(rel))


def is_blocked_indexer(rel) -> bool:
    return any(_BLOCKED.search(r) for r in _rejections(rel))


def in_size_band(rel, policy) -> bool:
    gb = (rel.get("size") or 0) / 1e9
    lo = float(policy.get("min_size_gb", 0))
    hi = float(policy.get("max_size_gb", 1e9))
    return gb == 0 or (lo <= gb <= hi)  # size 0 = unknown → pass


def quality_rank(rel) -> int:
    q = (rel.get("quality") or {}).get("quality") or {}
    qw = rel.get("qualityWeight")
    return int(qw if qw is not None else (q.get("id") or 0))


def select_replacement(candidates, policy):
    """Best grabbable candidate, or (None, reason). Rejects only what's decidable from the release
    metadata; authenticity ('actually the claimed quality') is enforced post-download by is_authentic.
    NEVER rejects on codec or resolution — quality range is the Sonarr profile's job."""
    deny = policy.get("deny_release_groups", [])
    viable = [
        rel
        for rel in (candidates or [])
        if not matches_any(rel.get("title"), deny)
        and not is_wrong_map(rel)
        and not is_blocked_indexer(rel)
        and in_size_band(rel, policy)
    ]
    if not viable:
        return None, "no grabbable clean candidate"
    prefer_g = policy.get("prefer_release_groups", [])
    prefer_ix = policy.get("prefer_indexers", [])
    deprio = policy.get("depreference_codecs", [])

    def key(rel):
        title = rel.get("title", "")
        return (
            -quality_rank(rel),  # higher tier first
            1 if matches_any(title, deprio) else 0,  # within tier: non-AV1 before AV1
            -(rel.get("customFormatScore") or 0),  # then CF
            0 if matches_any(title, prefer_g) else 1,  # then preferred group
            0 if matches_any(rel.get("indexer"), prefer_ix) else 1,
            -(rel.get("seeders") or 0),  # then seeders
        )

    return sorted(viable, key=key)[0], "selected"


def is_authentic(probe, policy) -> bool:
    """False iff a downloaded file betrays a mislabeled re-encode: it claims a stream-copy tier
    (Remux) but its video stream is a consumer re-encode (re-encoder ENCODER tag or long GOP). A
    non-remux claim (WEB-DL / Bluray encode) legitimately carries an encoder tag → authentic. Codec-
    and resolution-agnostic — the gate is about honesty, not format."""
    quality = probe.get("quality")
    if not frl.is_remux_quality(quality):
        return True
    ev = frl.reencode_evidence(
        quality,
        probe.get("encoder"),
        probe.get("keyframes"),
        int(probe.get("window_s", 40)),
        float(policy.get("gop_max_s", 5)),
        frl.DEFAULT_RE_ENCODER_MARKERS,
    )
    return ev is None
