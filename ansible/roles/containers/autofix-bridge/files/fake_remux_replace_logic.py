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


def plan_searches(ledger, policy):
    """Up to searches_per_tick 'detected' entries to interactive-search this tick, oldest first
    (deterministic). The shell spaces the searches by search_spacing_s and never issues a season
    search. Returns search-action dicts; does not mutate state (the shell sets grabbed/held/detected
    after the grab, so a crash mid-tick simply leaves the entry 'detected' for the next tick)."""
    per_tick = int(policy.get("searches_per_tick", 2))
    detected = sorted(
        (r for r in ledger.values() if r["state"] == "detected"),
        key=lambda r: (r.get("firstSeen", 0), r["episodeId"]),
    )
    return [
        {"type": "search", "episodeId": r["episodeId"], "seriesId": r["seriesId"]}
        for r in detected[:per_tick]
    ]


def _set(ledger, ep, **changes):
    rec = dict(ledger[str(ep)])
    rec.update(changes)
    return {**ledger, str(ep): rec}


def advance(ledger, queue_by_dlid, files_by_ep, probes, policy, now):
    """Advance grabbed→verifying→importing→replaced from observed reality. Pure: the shell has
    already grabbed (recording chosen.downloadId), ffprobed completed downloads (into `probes`), and
    read the queue + current files. Emits delete_file / import / blocklist / alert for the shell to
    execute (only in live mode). No delete is ever emitted unless the download is complete AND
    authentic."""
    stall_s = float(policy.get("download_stall_hours", 12)) * 3600
    max_attempts = int(policy.get("max_attempts", 3))
    actions = []
    out = dict(ledger)
    for ep, rec in ledger.items():
        state = rec["state"]
        if state == "grabbed":
            dlid = (rec.get("chosen") or {}).get("downloadId")
            item = queue_by_dlid.get(dlid) if dlid else None
            if item is None:  # vanished/failed from the download client
                attempts = rec.get("attempts", 0) + 1
                if attempts >= max_attempts:
                    out = _set(
                        out,
                        ep,
                        state="held",
                        attempts=attempts,
                        reason="grab lost",
                        lastAction=now,
                    )
                else:
                    out = _set(
                        out,
                        ep,
                        state="detected",
                        attempts=attempts,
                        chosen=None,
                        reason="grab lost",
                        lastAction=now,
                    )
                continue
            if item.get("sizeleft", 1) > 0:  # still downloading
                if now - rec.get("lastAction", now) > stall_s:
                    out = _set(
                        out, ep, state="held", reason="download stalled", lastAction=now
                    )
                continue
            # sizeleft == 0 → fully downloaded → verify (probe supplied by the shell)
            probe = probes.get(dlid)
            if probe is None:
                out = _set(out, ep, state="verifying", lastAction=now)
                continue
            if is_authentic(probe, policy):
                actions.append(
                    {
                        "type": "delete_file",
                        "episodeId": rec["episodeId"],
                        "fileId": rec["fakeFileId"],
                    }
                )
                actions.append({"type": "import", "episodeId": rec["episodeId"]})
                out = _set(out, ep, state="importing", lastAction=now)
            else:
                attempts = rec.get("attempts", 0) + 1
                actions.append(
                    {
                        "type": "blocklist",
                        "episodeId": rec["episodeId"],
                        "downloadId": dlid,
                    }
                )
                if attempts >= max_attempts:
                    out = _set(
                        out,
                        ep,
                        state="held",
                        attempts=attempts,
                        reason="replacement also fake",
                        lastAction=now,
                    )
                else:
                    out = _set(
                        out,
                        ep,
                        state="detected",
                        attempts=attempts,
                        chosen=None,
                        reason="replacement also fake",
                        lastAction=now,
                    )
        elif state == "verifying":
            # re-entrant: same logic as the grabbed→verify branch once a probe arrives
            dlid = (rec.get("chosen") or {}).get("downloadId")
            probe = probes.get(dlid)
            if probe is None:
                continue
            if is_authentic(probe, policy):
                actions.append(
                    {
                        "type": "delete_file",
                        "episodeId": rec["episodeId"],
                        "fileId": rec["fakeFileId"],
                    }
                )
                actions.append({"type": "import", "episodeId": rec["episodeId"]})
                out = _set(out, ep, state="importing", lastAction=now)
            else:
                attempts = rec.get("attempts", 0) + 1
                actions.append(
                    {
                        "type": "blocklist",
                        "episodeId": rec["episodeId"],
                        "downloadId": dlid,
                    }
                )
                nxt = "held" if attempts >= max_attempts else "detected"
                out = _set(
                    out,
                    ep,
                    state=nxt,
                    attempts=attempts,
                    chosen=None,
                    reason="replacement also fake",
                    lastAction=now,
                )
        elif state == "importing":
            cur = files_by_ep.get(str(rec["episodeId"]))
            if cur is not None and cur != rec["fakeFileId"]:
                out = _set(out, ep, state="replaced", lastAction=now)
            elif (
                now - rec.get("lastAction", now) > 3600
            ):  # import didn't land within an hour
                out = _set(
                    out,
                    ep,
                    state="held",
                    reason="import did not complete",
                    lastAction=now,
                )
    return out, actions
