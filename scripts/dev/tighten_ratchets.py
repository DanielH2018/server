#!/usr/bin/env python3
"""Lower every ratchet allowlist entry to what its file is today.

The two lists are `ansible/tests/repo/module_length_allowlist.txt` and
`ansible/tests/repo/monkeypatch_allowlist.txt`, and an entry has to MATCH its file (the
`DECIDED:` marker in `ansible/tests/_ratchet.py`). So every PR that shrinks a listed file
also edits that file's line: 70 commits did in the 30 days to 2026-09-28, 49 of them only
lowering or deleting. This writes that edit instead.

It only ever tightens. An entry over its file's count falls to the count, an entry whose file
is gone or back under its cap is deleted, and an entry a file has GROWN past is left exactly
as written so `test_no_module_is_longer_than_its_cap_or_its_allowlist_entry` still fails.

It writes unconditionally, without running the suite first, which is what keeps it out of the
#1799 repair deadlock — the ratchet it repairs is red exactly when the repair is needed.
`--tighten` writes and exits 1 when it rewrote a list, the way a formatter does, so the prek
hook stages what it wrote. Without the flag it only reports, and exits 1 when a list needs the
edit.

Typical usage example:

  uv run --frozen python scripts/dev/tighten_ratchets.py --tighten
"""

import sys as _sys
from pathlib import Path as _Path

# `scripts` for the `lib.yaml_fast` import `_helpers` makes, and `ansible/tests` for
# `_ratchet_census` and `_ratchet` themselves. Both are pytest `pythonpath` entries, which a
# direct invocation never reads.
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2] / "ansible" / "tests"))

import argparse

from _helpers import REPO
from _ratchet import Ratchet, parse_allowlist, tighten
from _ratchet_census import LENGTHS, PATCHES, line_counts, monkeypatch_counts


def changes(before: str, after: str) -> list[str]:
    """One sentence per entry the rewrite lowers or deletes."""
    old, new = parse_allowlist(before), parse_allowlist(after)
    return [
        f"  {path} {limit} -> {new[path]}"
        if path in new
        else f"  {path} {limit} -> gone"
        for path, limit in old.items()
        if new.get(path) != limit
    ]


def tighten_list(ratchet: Ratchet, counts: dict[str, int], *, write: bool) -> list[str]:
    """Tighten one list, writing it when asked. Returns the changes, empty when clean."""
    before = ratchet.path.read_text()
    after = tighten(before, counts, ratchet.cap_of)
    if after == before:
        return []
    if write:
        ratchet.path.write_text(after)
    return changes(before, after)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--tighten",
        action="store_true",
        help="rewrite the lists rather than only reporting what would change",
    )
    args = parser.parse_args()

    edited = False
    for ratchet, counts in ((LENGTHS, line_counts()), (PATCHES, monkeypatch_counts())):
        found = tighten_list(ratchet, counts, write=args.tighten)
        if found:
            edited = True
            verb = "tightened" if args.tighten else "needs tightening"
            print(f"{ratchet.path.relative_to(REPO)}: {verb}")
            print("\n".join(found))
    if edited and args.tighten:
        print("Stage the rewritten list(s) and commit again.")
    return 1 if edited else 0


if __name__ == "__main__":
    raise SystemExit(main())
