"""Tests for memory_survey.

Every rule gets a `..._is_clean` / `..._is_flagged` pair. A survey that flagged everything and
one that flagged nothing are indistinguishable from the passing side alone, so each rule needs
one input it must accept and one it must reject — the repo's red-proof rule, applied per rule
rather than per script.
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import json

from dev import memory_survey


def _mem(tmp_path, index: str, files: dict[str, str]) -> _Path:
    d = tmp_path / "memory"
    d.mkdir(parents=True)
    (d / "MEMORY.md").write_text(index, encoding="utf-8")
    for name, body in files.items():
        (d / name).write_text(body, encoding="utf-8")
    return d


def _survey(d: _Path, transcripts: _Path | None = None, days: int = 30):
    return memory_survey.survey(d, transcripts or (d / "__none__"), days)


# --- dead index links: the only condition that fails the run ---------------------


def test_dead_link_is_clean_when_every_pointer_resolves(tmp_path):
    d = _mem(tmp_path, "- [A](a.md) — hook\n", {"a.md": "body one"})
    assert _survey(d)["dead_links"] == []


def test_dead_link_is_flagged_when_a_pointer_names_a_missing_file(tmp_path):
    d = _mem(tmp_path, "- [A](a.md)\n- [Gone](gone.md)\n", {"a.md": "body one"})
    assert _survey(d)["dead_links"] == ["gone.md"]


def test_dead_link_sets_exit_code_and_orphans_do_not(tmp_path, capsys):
    d = _mem(tmp_path, "- [Gone](gone.md)\n", {"a.md": "body"})
    assert memory_survey.main(["--memory-dir", str(d)]) == 1
    capsys.readouterr()

    clean = _mem(tmp_path / "second", "- [A](a.md)\n", {"a.md": "b", "orphan.md": "c"})
    assert memory_survey.main(["--memory-dir", str(clean)]) == 0
    capsys.readouterr()


# --- orphans -------------------------------------------------------------------


def test_orphan_is_clean_when_the_index_links_every_file(tmp_path):
    d = _mem(tmp_path, "- [A](a.md)\n- [B](b.md)\n", {"a.md": "x", "b.md": "y"})
    assert _survey(d)["orphans"] == []


def test_orphan_is_flagged_when_a_file_has_no_pointer(tmp_path):
    d = _mem(tmp_path, "- [A](a.md)\n", {"a.md": "x", "stray.md": "y"})
    assert _survey(d)["orphans"] == ["stray.md"]


def test_index_itself_is_never_reported_as_an_orphan(tmp_path):
    d = _mem(tmp_path, "- [A](a.md)\n", {"a.md": "x"})
    s = _survey(d)
    assert "MEMORY.md" not in s["orphans"]
    assert s["store"]["files"] == 1


def test_a_link_written_with_a_directory_prefix_still_resolves(tmp_path):
    d = _mem(tmp_path, "- [A](./a.md)\n", {"a.md": "x"})
    s = _survey(d)
    assert s["dead_links"] == []
    assert s["orphans"] == []


# --- duplicate candidates ------------------------------------------------------


def test_duplicates_are_clean_when_bodies_share_no_phrasing(tmp_path):
    d = _mem(
        tmp_path,
        "",
        {
            "a.md": "longhorn refuses a volume size that is not a block multiple",
            "b.md": "pihole answers aaaa queries with a null address wedging grpc",
        },
    )
    assert _survey(d)["duplicate_candidates"] == []


def test_duplicates_are_flagged_when_two_entries_restate_one_fact(tmp_path):
    shared = "the gitops tick pulls all of master not just your commit so another session work lands too"
    d = _mem(tmp_path, "", {"a.md": shared, "b.md": shared + " and then deploys it"})
    pairs = _survey(d)["duplicate_candidates"]
    assert pairs and {pairs[0][0], pairs[0][1]} == {"a.md", "b.md"}


def test_frontmatter_is_excluded_from_duplicate_scoring(tmp_path):
    # Identical frontmatter, unrelated bodies. Scoring the frontmatter would match these.
    fm = "---\nname: x\ndescription: a memory about the homelab deploy pipeline\n---\n"
    d = _mem(
        tmp_path,
        "",
        {
            "a.md": fm + "longhorn refuses a size that is not a block multiple",
            "b.md": fm + "cron inherits neither path nor kubeconfig from the shell",
        },
    )
    assert _survey(d)["duplicate_candidates"] == []


# --- last-referenced -----------------------------------------------------------


def _assistant(text: str) -> str:
    return json.dumps(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}
    )


def _tool_result(text: str) -> str:
    return json.dumps(
        {
            "type": "user",
            "message": {"content": [{"type": "tool_result", "content": text}]},
        }
    )


def _transcripts(tmp_path, lines: list[str]) -> _Path:
    t = tmp_path / "transcripts"
    t.mkdir(parents=True)
    (t / "session.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return t


def test_reference_is_found_when_a_transcript_names_the_slug(tmp_path):
    name = "longhorn-retain-is-per-job.md"
    d = _mem(tmp_path, f"- [Slug]({name})\n", {name: "x"})
    t = _transcripts(
        tmp_path,
        [_assistant("per longhorn-retain-is-per-job, retain is per job")],
    )
    assert _survey(d, t)["unreferenced"] == []


def test_a_short_slug_matches_on_any_substring(tmp_path):
    # Pinning a known weakness rather than hiding it: the scan is a substring test, so a
    # one-character slug reads as referenced by almost any transcript. Real memory slugs are
    # long kebab-case phrases, which is what makes the scan sound in practice. A future move
    # to short slugs would silently mark every entry live, and this test is what would fail.
    d = _mem(tmp_path, "- [A](a.md)\n", {"a.md": "x"})
    t = _transcripts(tmp_path, [_assistant("see a for the answer")])
    assert _survey(d, t)["unreferenced"] == []


def test_a_line_carrying_the_whole_index_does_not_count_as_a_reference(tmp_path):
    # MEMORY.md is injected verbatim into every session, so it lands in every transcript. Without
    # the bulk-line skip, every indexed entry reads as referenced today and the signal measures
    # the injection rather than use. This is the rejecting half of that rule.
    names = [f"entry-number-{i}-about-something.md" for i in range(8)]
    d = _mem(tmp_path, "", {n: "x" for n in names})
    injected = " ".join(_Path(n).stem for n in names)
    t = _transcripts(tmp_path, [_assistant(injected)])
    assert _survey(d, t)["unreferenced"] == sorted(names)


def test_a_line_citing_two_slugs_still_counts_as_a_reference(tmp_path):
    # The accepting half: a sentence that genuinely cites a couple of related memories is a
    # reference, and must not be swept up by the bulk-line skip.
    a, b = "cron-path-omits-usr-local-bin.md", "grace-periods-must-be-derived.md"
    d = _mem(tmp_path, "", {a: "x", b: "y"})
    t = _transcripts(
        tmp_path,
        [
            _assistant(
                "see cron-path-omits-usr-local-bin and grace-periods-must-be-derived"
            )
        ],
    )
    assert _survey(d, t)["unreferenced"] == []


def test_a_tool_result_naming_the_slug_is_not_a_reference(tmp_path):
    # A directory listing of the memory store names every file, one per line, so it clears the
    # bulk-line skip. Only the assistant's own words count as a citation — this is what stopped
    # the real store reporting all 107 entries as referenced today.
    name = "cron-path-omits-usr-local-bin.md"
    d = _mem(tmp_path, f"- [A]({name})\n", {name: "x"})
    t = _transcripts(tmp_path, [_tool_result(f"{name}\nsome-other-file.md")])
    assert _survey(d, t)["unreferenced"] == [name]


def test_unreferenced_when_no_transcript_mentions_the_slug(tmp_path):
    d = _mem(
        tmp_path, "- [A](never-cited-anywhere.md)\n", {"never-cited-anywhere.md": "x"}
    )
    t = _transcripts(tmp_path, [_assistant("unrelated chatter")])
    assert _survey(d, t)["unreferenced"] == ["never-cited-anywhere.md"]


def test_a_transcript_outside_the_window_does_not_count_as_a_reference(tmp_path):
    import os
    import time

    d = _mem(tmp_path, "- [A](aged-out-entry.md)\n", {"aged-out-entry.md": "x"})
    t = _transcripts(tmp_path, [_assistant("aged-out-entry was useful once")])
    old = time.time() - 60 * 86400
    os.utime(t / "session.jsonl", (old, old))

    assert _survey(d, t, days=30)["unreferenced"] == ["aged-out-entry.md"]
    assert _survey(d, t, days=90)["unreferenced"] == []


def test_an_unreadable_transcript_leaves_the_slug_unreferenced(tmp_path):
    # Failing closed matters: a transcript we cannot read is missing evidence, and the safe
    # direction is to surface the entry for review rather than silently call it referenced.
    d = _mem(tmp_path, "- [A](some-entry.md)\n", {"some-entry.md": "x"})
    t = _transcripts(tmp_path, [_assistant("some-entry was cited")])
    (t / "session.jsonl").chmod(0o000)
    try:
        assert _survey(d, t)["unreferenced"] == ["some-entry.md"]
    finally:
        (t / "session.jsonl").chmod(0o644)


# --- reported cost -------------------------------------------------------------


def test_index_cost_counts_the_index_and_the_store_excludes_it(tmp_path):
    # The em-dash is three bytes and one character. The cost this reports is what the model
    # is charged, which tracks bytes, so the assertion has to encode rather than use len().
    index = "- [A](a.md) — hook\n"
    d = _mem(tmp_path, index, {"a.md": "body"})
    s = _survey(d)
    assert s["index"]["bytes"] == len(index.encode("utf-8"))
    assert s["store"]["bytes"] == len(b"body")
    assert s["index"]["pointer_links"] == 1


def test_a_link_repeated_in_the_index_is_counted_once(tmp_path):
    d = _mem(tmp_path, "- [A](a.md)\nsee also [again](a.md)\n", {"a.md": "x"})
    assert _survey(d)["index"]["pointer_links"] == 1
