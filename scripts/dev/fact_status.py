#!/usr/bin/env python3
"""Status, verification and lint for the fact stores — the one entry point.

``status`` derives every CLAUDE.md section's status from the tree and ``docs/facts.lock``
and exits 1 when any section is OUT. ``verify`` re-hashes the named sections' atoms into
the lock at HEAD — the only path from OUT back to IN — and ``--unverified`` does it for every
section that has no row yet. ``reverify`` is the prek hook's half: it re-hashes a section
whose citation set an edit changed, and refuses one whose recorded atom moved. ``forget`` drops a lock row whose
section no longer exists, which is the way out of a ``section-gone`` finding: a renamed
heading is a new unit, and the old row cannot be hand-deleted without tripping the lock's
own checksum. ``lint`` reports citations that cannot be support. Repo store only until
slice 4 adds ``--store memory``.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import argparse
from pathlib import Path

from lib.facts.lint import changed_units, lint_sections
from lib.facts.lock import (
    LOCK_REL,
    build_repo_edb,
    check_lock,
    forget_units,
    read_lock,
    repo_citations,
    reverify_benign,
    verify_units,
)
from lib.facts.relations import derive, status_of
from lib.git import git_stdout
from lib.repo_paths import REPO

_USAGE = 2


def cmd_status(args: argparse.Namespace) -> int:
    repo = Path(args.repo)
    lock_path = repo / LOCK_REL
    # One parse of the docs feeds the status, the lock check and the unit list. Every cited
    # unit is a section key, so unioning the cited set in adds nothing; two sections with
    # one heading text share a key here, and the `duplicate-heading` lint names them.
    by_unit = repo_citations(repo)
    edb = build_repo_edb(repo, read_lock(lock_path), by_unit)
    idb = derive(edb)
    for u in sorted(by_unit):
        print(f"{status_of(edb, idb, u)}  {u}")
    findings = check_lock(repo, lock_path, by_unit)
    for f in findings:
        print(f"  {f.kind}: {f.unit} {f.atom} — {f.detail}")
    for u, a in sorted(idb.one_way):
        print(f"  one-way: {u} cites {a} which carries no `# fact: {u}`")
    return 1 if idb.out or findings else 0


def cmd_verify(args: argparse.Namespace) -> int:
    repo = Path(args.repo)
    by_unit = repo_citations(repo)
    units = list(args.units)
    if args.unverified:
        # Sections with NO lock row only. The flag can therefore never launder a `moved`
        # atom — a verified section is untouched by it, whatever state it is in — so
        # raising coverage in bulk is safe in a way re-verifying in bulk would not be.
        recorded = set(read_lock(repo / LOCK_REL))
        fresh = sorted(set(by_unit) - recorded - set(units))
        # A section citing nothing is a convention and never had a row to write. A section
        # citing only a probe gets an empty row: the row is what moves it from UNVERIFIED to
        # UNKNOWN, since `relations` reads the recorded units from the lock's keys.
        units += [u for u in fresh if by_unit[u]]
    if not units:
        if not args.unverified:
            print("name a section, or pass --unverified", file=_sys.stderr)
            return _USAGE
        print("every section that cites an atom already has a lock row")
        return 0
    args.units = units
    unknown = [u for u in units if u not in by_unit]
    if unknown:
        print(
            f"no section named {unknown}; `fact_status.py status` lists the keys",
            file=_sys.stderr,
        )
        return _USAGE
    if git_stdout("status", "--porcelain", cwd=repo):
        print(
            "working tree is dirty; verified_sha names HEAD, not this state",
            file=_sys.stderr,
        )
    head = git_stdout("rev-parse", "--short=9", "HEAD", cwd=repo)
    lock, skipped = verify_units(repo, repo / LOCK_REL, args.units, head, by_unit)
    for u in args.units:
        print(f"verified {u} at {head}: {len(lock[u]['atoms'])} atoms")
    if skipped:
        print(f"skipped {len(skipped)} unresolved: {', '.join(skipped)}")
    return 0


def cmd_reverify(args: argparse.Namespace) -> int:
    """Re-hash the edited sections whose findings are benign; refuse the rest.

    The prek hook's entry point. It exits 1 when it wrote the lock, the way a formatter does:
    the commit stops, the author stages the lock the hook just wrote, and the re-hash lands in
    the same commit as the edit that needed it rather than in a follow-up.
    """
    repo = Path(args.repo)
    try:
        changed = changed_units(repo, args.changed_since)
    except ValueError as unresolvable:
        print(unresolvable, file=_sys.stderr)
        return _USAGE
    head = git_stdout("rev-parse", "--short=9", "HEAD", cwd=repo)
    done, blocking = reverify_benign(repo, repo / LOCK_REL, changed, head)
    # Two headings, because the remedy differs. A finding in a section this commit edits is
    # one the author is looking at. A finding elsewhere is CI's next failure, and saying so
    # here is cheaper than letting them push and read it from the run.
    here = [f for f in blocking if f.unit in changed or not f.unit]
    elsewhere = [f for f in blocking if f not in here]
    for f in here:
        print(f"{f.kind}: {f.unit} {f.atom} — {f.detail}", file=_sys.stderr)
    if elsewhere:
        print("not folded, and CI fails on these:", file=_sys.stderr)
        for f in elsewhere:
            print(f"  {f.kind}: {f.unit} {f.atom} — {f.detail}", file=_sys.stderr)
    for u in done:
        print(
            f"re-verified {u} at {head}: its citation set changed, no recorded atom moved"
        )
    if done:
        print(
            f"\n{LOCK_REL} rewritten — `git add {LOCK_REL}` and commit again.",
            file=_sys.stderr,
        )
    return 1 if done or blocking else 0


def cmd_forget(args: argparse.Namespace) -> int:
    repo = Path(args.repo)
    try:
        forget_units(repo / LOCK_REL, args.units)
    except KeyError as missing:
        print(
            f"no lock row for {missing.args[0]!r}; `fact_status.py status` lists the keys",
            file=_sys.stderr,
        )
        return _USAGE
    for u in args.units:
        print(f"forgot {u}")
    return 0


def cmd_lint(args: argparse.Namespace) -> int:
    repo = Path(args.repo)
    try:
        keys = changed_units(repo, args.changed_since) if args.changed_since else None
    except ValueError as unresolvable:
        print(unresolvable, file=_sys.stderr)
        return _USAGE
    findings = lint_sections(repo, keys, since=args.changed_since)
    for f in findings:
        print(f"{'warn ' if f.warn else 'ERROR'} {f.rule:<20} {f.unit}: {f.detail}")
    return 1 if any(not f.warn for f in findings) else 0


def _add_common(sp: argparse.ArgumentParser) -> None:
    """Add ``--repo``/``--store`` to a subparser.

    On the top-level parser alone these must precede the subcommand token
    (``fact_status.py --repo P status``); every caller here writes the flag
    after the subcommand instead (``status --repo P``), which argparse only
    accepts when each subparser carries its own copy.
    """
    sp.add_argument("--repo", default=str(REPO))
    sp.add_argument("--store", choices=["repo"], default="repo")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("status")
    _add_common(st)
    st.set_defaults(fn=cmd_status)
    v = sub.add_parser("verify")
    _add_common(v)
    v.add_argument("units", nargs="*")
    v.add_argument(
        "--unverified",
        action="store_true",
        help="also verify every section that cites atoms and has no lock row yet",
    )
    v.set_defaults(fn=cmd_verify)
    rv = sub.add_parser("reverify")
    _add_common(rv)
    rv.add_argument("--changed-since", default="origin/master")
    rv.set_defaults(fn=cmd_reverify)
    fg = sub.add_parser("forget")
    _add_common(fg)
    fg.add_argument("units", nargs="+")
    fg.set_defaults(fn=cmd_forget)
    ln = sub.add_parser("lint")
    _add_common(ln)
    ln.add_argument("--changed-since", default=None)
    ln.set_defaults(fn=cmd_lint)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
