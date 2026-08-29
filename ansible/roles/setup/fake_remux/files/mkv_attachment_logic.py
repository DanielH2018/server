#!/usr/bin/env python3
"""Pure logic for the mkv attachment-name repair — no ffprobe, no mkvpropedit, no filesystem.

jellyfin-ffmpeg 7.1 hardened attachment dumping with a `safe_filename()` check that accepts only
[A-Za-z0-9_.-] (fftools/ffmpeg_demux.c). A font attachment named `Nexa Bold.otf` fails it, and
ffmpeg aborts the WHOLE input rather than skipping that one attachment — so every burned-in-subtitle
transcode of that file returns a "source error" while direct play stays fine. 21 of 61 mkv files in
the library carried such a name on 2026-08-29.

The split exists so this half is testable: an mkv is a binary no test can commit, so the entry
script owns the probe and the rewrite and this module owns every decision they make.
"""

from __future__ import annotations

import re

# The exact character class ffmpeg's safe_filename() accepts. A leading '.' is excluded here on
# purpose — ffmpeg allows it, but a dotfile attachment name is a needless trap for the shell
# pipelines that later handle a dumped font.
SAFE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9_.-]*$")


def is_safe(name: str) -> bool:
    """True when ffmpeg 7.1 would accept this attachment filename as-is."""
    return bool(SAFE.match(name))


def sanitize(name: str, taken: set[str]) -> str:
    """Map `name` into [A-Za-z0-9_.-], keeping the extension and guaranteeing a non-empty stem.

    `taken` is the set of already-claimed lowercase names; it is mutated so successive calls in one
    file cannot collide. Collision matters: `Nexa Bold.otf` and `Nexa-Bold.otf` both sanitize to
    `Nexa_Bold.otf`, and mkvpropedit selects the attachment to rewrite BY NAME, so two attachments
    sharing one name make the second rewrite ambiguous.
    """
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    stem = re.sub(r"[^A-Za-z0-9_-]", "_", stem)
    ext = re.sub(r"[^A-Za-z0-9]", "_", ext)
    if not stem or not re.match(r"[A-Za-z0-9_-]", stem[0]):
        stem = "f" + stem
    cand = f"{stem}.{ext}" if ext else stem
    n = 1
    while cand.lower() in taken:
        cand = f"{stem}_{n}.{ext}" if ext else f"{stem}_{n}"
        n += 1
    assert SAFE.match(cand), cand
    taken.add(cand.lower())
    return cand


def plan(names: list[str]) -> list[tuple[str, str]]:
    """[(old, new)] for every attachment ffmpeg would reject. Empty list means the file is fine.

    Safe names are seeded into `taken` first, so a rename never lands on a name the file already
    uses.
    """
    taken = {n.lower() for n in names if is_safe(n)}
    return [(n, sanitize(n, taken)) for n in names if not is_safe(n)]


def mkvpropedit_args(renames: list[tuple[str, str]]) -> list[str]:
    """The argv tail that renames each attachment in the mkv header.

    Order is load-bearing and mkvpropedit does not warn about it: the property options
    (`--attachment-name`) must PRECEDE the `--update-attachment` selector they apply to.
    """
    args: list[str] = []
    for old, new in renames:
        args += ["--attachment-name", new, "--update-attachment", f"name:{old}"]
    return args


def verdict(scanned: int, repaired: int, failures: list[str]) -> tuple[bool, str]:
    """(ok, msg) for the state file monitor-bridge's Kuma push reads.

    `scanned` is in the message and a zero count is DOWN, both deliberately. Once the library is
    clean this check finds nothing on every run forever, so a scan that silently stopped SEEING
    files — the PV unmounted, the root moved, the glob wrong — is otherwise indistinguishable from
    a clean library and both read green. That is the failure mode this role's defaults already name
    ("the failure mode of a wrong path here is silence, not an error"), so the count is the signal
    and "ok" alone is not.
    """
    if failures:
        return False, "%d scanned, %d repaired, %d FAILED: %s" % (
            scanned,
            repaired,
            len(failures),
            "; ".join(failures[:3]),
        )
    if scanned == 0:
        return False, "no mkv files found — media root missing or unmounted?"
    if repaired:
        return True, "%d scanned, %d repaired" % (scanned, repaired)
    return True, "%d scanned, 0 unsafe" % scanned
