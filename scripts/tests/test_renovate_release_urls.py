"""Guard that every version embedded in a release-download URL is tracked by a manager.

A `releases/download/vX.Y.Z/` URL in a task or template is a pin Renovate only sees through
a custom manager scanning that file. This walks every tracked file holding such a URL,
subtracts the spans the managers cover, and fails on what is left -- plus a corpus floor,
so a manager that quietly stopped matching cannot empty the corpus and pass.

Run: uv run pytest scripts/tests/test_renovate_release_urls.py
"""

from __future__ import annotations

import re


from _renovate import (
    _MANAGERS,
    _REPO,
    _file_pattern_to_regex,
    _to_python_regex,
)


_RELEASE_URL_RE = re.compile(r"releases/download/v([\d.]+)/")

# The floor the derived corpus may not drop below. Every file here holds a release-download
# pin today; the derivation reads managerFilePatterns, so deleting a manager would quietly take
# its file out of the corpus and turn a now-untracked pin green. Assert the floor separately
# and that failure mode fails CI instead.
_RELEASE_URL_CORPUS_FLOOR = frozenset(
    {
        ".github/workflows/ci.yml",
        ".vale.ini",
        "ansible/roles/k8s/jellyfin/defaults/main.yml",
    }
)

# Files that hold a release-download URL no manager scans, and legitimately so. Two rules,
# not a skip list — a new entry needs a reason of the same shape:
#   * Markdown is prose. A release URL in a doc is a citation, nothing renders or executes it,
#     and pointing a manager at a .md file would be the bug (docs/anilist-integration.md
#     names the ani-sync releases 11 times while discussing them).
#   * A file whose "URLs" are matchString regexes rather than pins: renovate.json itself, and
#     this module, whose release URLs are the synthetic fixtures of the red proofs below.
_RELEASE_URL_EXEMPT_FILES = frozenset(
    {"renovate.json", "scripts/tests/test_renovate_release_urls.py"}
)

# A version occurrence inside a COMMENT is prose, not a pin. No manager rewrites a comment, so
# demanding coverage there is unsatisfiable — jellyfin's defaults explain the pin in a comment
# that names the version, and that sentence must not be a CI failure. Every file type the
# corpus can hold comments the same way: YAML (workflows, role defaults), INI (.vale.ini),
# TOML, Dockerfile, and the shell inside a workflow's `run:` block all take `#` to end of line.
# Anchored to start-of-line or a preceding space, so a `#` fragment inside a URL is not one.
_COMMENT_RE = re.compile(r"(?m)(?:(?<=^)|(?<=\s))#[^\n]*")


def _is_release_url_exempt(path: str) -> bool:
    return path.endswith(".md") or path in _RELEASE_URL_EXEMPT_FILES


def _rewritable_text(text: str) -> str:
    """`text` with comments blanked out, same length, so offsets still line up with `text`."""
    return _COMMENT_RE.sub(lambda m: " " * len(m.group()), text)


def _managers_scanning(path: str, managers: list[dict]) -> list[dict]:
    return [
        m
        for m in managers
        if any(_file_pattern_to_regex(p).search(path) for p in m["managerFilePatterns"])
    ]


def _covered_spans(
    path: str, text: str, managers: list[dict] | None = None
) -> list[tuple[int, int]]:
    """Every span in `text` that some customManager scanning `path` actually matches."""
    spans: list[tuple[int, int]] = []
    for mgr in managers if managers is not None else _MANAGERS:
        if not any(
            _file_pattern_to_regex(p).search(path) for p in mgr["managerFilePatterns"]
        ):
            continue
        for ms in mgr["matchStrings"]:
            for m in re.finditer(_to_python_regex(ms), text):
                spans.append(m.span())
    return spans


def _uncovered_in_text(
    text: str, spans: list[tuple[int, int]]
) -> list[tuple[int, str]]:
    """(offset, version) for every occurrence of a pinned release version no span covers.

    The verdict every red proof below shares. A release-download URL carries its version more
    than once — `.../download/v3.18.0/vale_3.18.0_Linux_64-bit.tar.gz` — and a manager matching
    only the tag rewrites half of it on a bump, producing a 404. Asserting that the pin LINE is
    matched cannot see that; asserting that every occurrence of the captured version is matched
    can. Comments are blanked first (see _COMMENT_RE) so prose about the pin is not held to a
    coverage rule no manager could satisfy.

    Matching the version as a free substring is deliberate over-approximation: it also fires on
    `4.1` inside `4.1.0.0`, which is the point — jellyfin's four-part plugin version embeds the
    two-part release tag, and both have to move together.
    """
    code = _rewritable_text(text)
    captured = {m.group(1) for m in _RELEASE_URL_RE.finditer(code)}
    return [
        (occ.start(), version)
        for version in sorted(captured)
        for occ in re.finditer(re.escape(version), code)
        if not any(s <= occ.start() and occ.end() <= e for s, e in spans)
    ]


def _uncovered_version_occurrences(path: str) -> list[str]:
    text = (_REPO / path).read_text()
    uncovered = _uncovered_in_text(text, _covered_spans(path, text))
    return [
        f"{path}:{text[:offset].count(chr(10)) + 1}: {version}"
        for offset, version in uncovered
    ]


def _release_url_files(tracked: list[str]) -> list[str]:
    """Tracked files holding a `releases/download/v<version>/` pin, in path order."""
    out = []
    for f in tracked:
        p = _REPO / f
        # git tracks submodule gitlinks as paths that resolve to directories.
        if not p.is_file():
            continue
        if _RELEASE_URL_RE.search(p.read_text(errors="ignore")):
            out.append(f)
    return sorted(out)


def _partition_by_manager_coverage(
    paths: list[str], managers: list[dict]
) -> tuple[list[str], list[str]]:
    """Split `paths` into (scanned by some manager, scanned by none)."""
    scanned = [p for p in paths if _managers_scanning(p, managers)]
    return scanned, [p for p in paths if p not in set(scanned)]


def test_every_release_download_version_occurrence_is_tracked(
    tracked: list[str],
) -> None:
    """A pinned release URL must have EVERY occurrence of its version tracked, not just the tag.

    2026-08-31 review: the Vale binary was pinned with no manager at all, and the first proposed
    manager matched only the tag — which would have rewritten
    `.../download/v3.19.0/vale_3.18.0_Linux_64-bit.tar.gz`, a 404 that surfaced two lines later
    as `tar xzf`'s "not in gzip format" (the curl carried no `-f`; it does now), and the bump
    rides in a shared automerge group where it stalls every other non-major update bundled
    with it.

    The corpus was a hardcoded two-entry tuple until the 2026-09-01 review, which found the
    jellyfin-ani-sync pin untracked in a file the tuple never named. It is now derived: every
    tracked file that holds a release-download URL AND that some customManager scans. A file no
    manager scans has nothing to be PARTIALLY covered, so it is a different defect — the
    scanned-or-exempt test below is the one that catches it.

    What this does NOT gate: a bump that rewrites every version occurrence can still leave a
    404, because jellyfin's asset name also embeds the Jellyfin server targetAbi, which no
    datasource supplies. `automerge: false` plus the operator finishing the targetAbi and the
    MD5 is that defence, not this test.
    """
    corpus, _ = _partition_by_manager_coverage(_release_url_files(tracked), _MANAGERS)
    uncovered = [p for f in corpus for p in _uncovered_version_occurrences(f)]
    assert not uncovered, (
        "a pinned release version occurs where no customManager matchString reaches, so a "
        "Renovate bump would rewrite only part of the URL and leave a broken download:\n"
        + "\n".join(uncovered)
    )


def test_a_tag_only_matchstring_is_flagged() -> None:
    """The rejecting half: the tag-only manager must come back uncovered, or the check is inert."""
    text = (
        "          curl -fsSL -o /tmp/vale.tar.gz \\\n"
        "            https://github.com/vale-cli/vale/releases/download/v3.18.0/"
        "vale_3.18.0_Linux_64-bit.tar.gz\n"
    )
    tag_only = [
        m.span()
        for m in re.finditer(r"vale-cli/vale/releases/download/v([\d.]+)/", text)
    ]
    assert _uncovered_in_text(text, tag_only), (
        "the check no longer sees the asset-name occurrence a tag-only matchString misses"
    )


def test_a_version_occurrence_in_a_comment_is_clean() -> None:
    """The accepting half of the comment rule: prose naming the version must not be a failure.

    jellyfin's defaults explain the pin in a comment that says "4.1.0.0 declares 10.11.6.0 and
    loads" — no manager can rewrite that, so requiring coverage there would make the check
    permanently red for a sentence that is doing its job.
    """
    text = (
        "# PINNED one release behind: 4.1.0.0 declares targetAbi 10.11.6.0 and loads.\n"
        'anisync_url: "https://github.com/vosmiic/jellyfin-ani-sync/releases/download/v4.1/'
        '10.11.6.-.ani-sync_4.1.0.0.zip"\n'
    )
    spans = [
        m.span()
        for pattern in (
            r"jellyfin-ani-sync/releases/download/v(\d+\.\d+)/",
            r"ani-sync_(\d+\.\d+)\.0\.0\.zip",
        )
        for m in re.finditer(pattern, text)
    ]
    assert not _uncovered_in_text(text, spans), (
        "a version named in a comment is being demanded of a manager that cannot rewrite it"
    )


def test_release_url_corpus_holds_every_known_pinned_file(tracked: list[str]) -> None:
    """The derived corpus may widen but never shrink below the files known to hold a pin.

    The derivation reads managerFilePatterns, so retiring or re-pathing a manager silently
    drops its file out of the corpus and the now-untracked pin reads green. The floor makes
    that a failure instead.
    """
    corpus, _ = _partition_by_manager_coverage(_release_url_files(tracked), _MANAGERS)
    missing = sorted(_RELEASE_URL_CORPUS_FLOOR - set(corpus))
    assert not missing, (
        "a file known to hold a release-download pin dropped out of the derived corpus — no "
        "manager scans it any more, so its version occurrences are no longer checked at all:\n"
        + "\n".join(missing)
    )


def test_a_corpus_that_lost_its_manager_is_flagged(tracked: list[str]) -> None:
    """The rejecting half: drop the .vale.ini manager and .vale.ini must leave the corpus."""
    without_vale_styles = [
        m
        for m in _MANAGERS
        if not any("vale\\.ini" in p for p in m["managerFilePatterns"])
    ]
    corpus, _ = _partition_by_manager_coverage(
        _release_url_files(tracked), without_vale_styles
    )
    assert _RELEASE_URL_CORPUS_FLOOR - set(corpus) == {".vale.ini"}, (
        "removing the .vale.ini manager no longer takes .vale.ini out of the derived corpus — "
        "the floor test can no longer see a manager being retired out from under a pin"
    )


def test_every_release_url_file_is_scanned_or_exempt(tracked: list[str]) -> None:
    """A release-download pin in a file NO manager scans is the ani-sync defect (2026-09-01).

    That pin ages with no PR and no signal, and the occurrence test above cannot see it: with
    no manager scanning the file there is no partial coverage to detect. Exempt are Markdown
    (prose citing a release is not a pin) and the two files whose release URLs are matchString
    regexes or this module's own fixtures.
    """
    _, unscanned = _partition_by_manager_coverage(
        _release_url_files(tracked), _MANAGERS
    )
    offenders = [f for f in unscanned if not _is_release_url_exempt(f)]
    assert not offenders, (
        "a release-download pin sits in a file no Renovate customManager scans, so it ages "
        "with no update PR — add a manager, or a reasoned entry to _RELEASE_URL_EXEMPT_FILES "
        "if the URL is not a pin:\n" + "\n".join(offenders)
    )


def test_an_unscanned_release_url_file_is_flagged() -> None:
    """The rejecting half: a plausible new pin no manager reaches must come back unscanned.

    `roles/k8s/<svc>/vars/main.yml` is one directory off the pattern the k8s managers use —
    the same one-directory-short escape crowdsec_k8s_image and the k3s pins made before.
    """
    candidate = "ansible/roles/k8s/jellyfin/vars/main.yml"
    _, unscanned = _partition_by_manager_coverage([candidate], _MANAGERS)
    assert unscanned == [candidate] and not _is_release_url_exempt(candidate), (
        "a release pin one directory off the managed paths no longer reads as unscanned — the "
        "guard can no longer see an untracked pin"
    )
