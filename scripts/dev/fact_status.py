#!/usr/bin/env python3
"""Status, verification and lint for the fact stores — the one entry point.

``status`` derives every CLAUDE.md section's status from the tree and ``docs/facts.lock``
and exits 1 when any section is OUT. ``verify`` re-hashes the named sections' atoms into
the lock at HEAD — the only path from OUT back to IN. ``lint`` reports citations that cannot
be support. Repo store only until slice 4 adds ``--store memory``.

Spec: docs/superpowers/specs/2026-09-19-fact-support-invalidation-design.md
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import argparse
from pathlib import Path

from lib.facts.citations import repo_docs, sections
from lib.facts.lint import changed_units, lint_sections
from lib.facts.lock import LOCK_REL, build_repo_edb, check_lock, read_lock, verify_units
from lib.facts.relations import derive, status_of
from lib.git import git_stdout
from lib.repo_paths import REPO

_USAGE = 2


def cmd_status(args: argparse.Namespace) -> int:
    repo = Path(args.repo)
    lock_path = repo / LOCK_REL
    edb = build_repo_edb(repo, read_lock(lock_path))
    idb = derive(edb)
    units = sorted({u for u, _ in edb.cites} | {u for u in _all_units(repo)})
    for u in units:
        print(f"{status_of(edb, idb, u)}  {u}")
    findings = check_lock(repo, lock_path)
    for f in findings:
        print(f"  {f.kind}: {f.unit} {f.atom} — {f.detail}")
    for u, a in sorted(idb.one_way):
        print(f"  one-way: {u} cites {a} which carries no `# fact: {u}`")
    return 1 if idb.out or findings else 0


def _all_units(repo: Path) -> list[str]:
    return [
        s.key
        for d in repo_docs(repo)
        for s in sections(d.relative_to(repo).as_posix(), d.read_text())
    ]


def cmd_verify(args: argparse.Namespace) -> int:
    repo = Path(args.repo)
    known = set(_all_units(repo))
    unknown = [u for u in args.units if u not in known]
    if unknown:
        print(
            f"no section named {unknown}; `fact_status.py status` lists the keys",
            file=_sys.stderr,
        )
        return _USAGE
    head = git_stdout("rev-parse", "--short=9", "HEAD", cwd=repo)
    lock = verify_units(repo, repo / LOCK_REL, args.units, head)
    for u in args.units:
        print(f"verified {u} at {head}: {len(lock[u]['atoms'])} atoms")
    return 0


def cmd_lint(args: argparse.Namespace) -> int:
    repo = Path(args.repo)
    keys = changed_units(repo, args.changed_since) if args.changed_since else None
    findings = lint_sections(repo, keys)
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
    v.add_argument("units", nargs="+")
    v.set_defaults(fn=cmd_verify)
    ln = sub.add_parser("lint")
    _add_common(ln)
    ln.add_argument("--changed-since", default=None)
    ln.set_defaults(fn=cmd_lint)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
