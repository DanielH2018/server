"""Lint for the fact stores: the forms the spec excludes, and the atoms that do not resolve.

Two severities. An error is a citation that cannot be support (a rejected form, an atom that
does not resolve, an ambiguous marker) or a support record someone edited by hand. A warning
is a habit the spec asks writers to drop but a machine cannot judge (a number that may or may
not be a count, a test with no backref). ``fact_status.py lint`` exits non-zero on errors only.
"""

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .atoms import Ambiguous, backrefs, hash_atom
from .citations import (
    in_tree,
    parse_citations,
    repo_docs,
    sections,
    top_level_dirs,
)
from .lock import LOCK_REL, lock_tampered

RULES = frozenset(
    {
        "rejected-form",
        "dir-without-slash",
        "unresolved-atom",
        "ambiguous-marker",
        "one-way-test",
        "lock-tampered",
        "count-as-fact",
        "date-as-verification",
    }
)
WARN_RULES = frozenset({"count-as-fact", "one-way-test"})

_COUNT = re.compile(
    r"\b(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\s+"
    r"(?:entries|memories|roles|workloads|services|tests|monitors|files|hooks|checks)\b",
    re.I,
)
_DATE_CLAIM = re.compile(r"\bverified\b[^.\n]{0,40}\b\d{4}-\d{2}-\d{2}\b", re.I)


@dataclass(frozen=True)
class LintFinding:
    unit: str
    rule: str
    detail: str
    warn: bool


def _f(unit: str, rule: str, detail: str) -> LintFinding:
    return LintFinding(unit, rule, detail, rule in WARN_RULES)


def lint_sections(repo: Path, unit_keys: set[str] | None) -> list[LintFinding]:
    """Every ``RULES`` finding in the named sections, or in every section when ``unit_keys`` is None.

    Errors and warnings come back in one list; the caller splits them on ``LintFinding.warn``.
    Unlike ``lock.check_lock`` this reads no lock row: a never-verified section is linted the
    same as a verified one, which is what makes ``lint --changed-since`` a ratchet.
    """
    out: list[LintFinding] = []
    roots = top_level_dirs(repo)
    if lock_tampered(repo / LOCK_REL):
        out.append(
            _f(
                "",
                "lock-tampered",
                f"{LOCK_REL} checksum does not match; regenerate with `fact_status.py verify`",
            )
        )
    for doc in repo_docs(repo):
        rel = doc.relative_to(repo).as_posix()
        for sec in sections(rel, doc.read_text(encoding="utf-8")):
            if unit_keys is not None and sec.key not in unit_keys:
                continue
            cites, rejects = parse_citations(sec.body)
            # Out-of-tree spans are prose, not broken support: `origin/master` and a
            # doc-relative `defaults/main.yml` parse as paths and name nothing here. A
            # REJECTED form is not filtered — a line number is a claim about this tree
            # whatever its prefix — so the rejects list below is the unfiltered one.
            cites = [c for c in cites if in_tree(c, roots)]
            out.extend(
                _f(
                    sec.key,
                    "rejected-form",
                    f"`{r.raw}` is a {r.reason} citation; cite a symbol or marker",
                )
                for r in rejects
            )
            for c in cites:
                if c.form == "probe":
                    continue
                if (
                    c.form == "path"
                    and not c.path.endswith("/")
                    and (repo / c.path).is_dir()
                ):
                    out.append(
                        _f(
                            sec.key,
                            "dir-without-slash",
                            f"`{c.raw}` is a directory; cite it as `{c.raw}/`",
                        )
                    )
                    continue
                try:
                    resolved = hash_atom(c, repo) is not None
                except Ambiguous:
                    out.append(
                        _f(
                            sec.key,
                            "ambiguous-marker",
                            f"`{c.raw}` matches more than one line",
                        )
                    )
                    continue
                if not resolved:
                    out.append(
                        _f(sec.key, "unresolved-atom", f"`{c.raw}` does not resolve")
                    )
                elif c.form == "test" and sec.key not in backrefs(c, repo):
                    out.append(
                        _f(
                            sec.key,
                            "one-way-test",
                            f"`{c.raw}` carries no `# fact: {sec.key}`",
                        )
                    )
            for m in _COUNT.finditer(sec.body):
                out.append(
                    _f(
                        sec.key,
                        "count-as-fact",
                        f"'{m.group(0)}' is a count; render it or drop it",
                    )
                )
            for m in _DATE_CLAIM.finditer(sec.body):
                out.append(
                    _f(
                        sec.key,
                        "date-as-verification",
                        f"'{m.group(0)}': the lock's verified_sha is the only stamp",
                    )
                )
    return out


def changed_units(repo: Path, since: str) -> set[str]:
    """The sections whose text differs between ``since`` and the working tree.

    Raises ``ValueError`` when ``since`` names no commit. This is the prek hook's ratchet and
    it runs against ``origin/master``, which a shallow or freshly cloned checkout may not
    have: without the check, ``git show`` fails per document, every ``before`` map comes back
    empty, and every section in the repo reads as changed — the hook then lints the whole
    tree and reports a wall of errors that names nothing the commit touched.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    resolved = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{since}^{{commit}}"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if resolved.returncode != 0:
        raise ValueError(f"cannot resolve {since!r}")
    changed: set[str] = set()
    for doc in repo_docs(repo):
        rel = doc.relative_to(repo).as_posix()
        old = subprocess.run(
            ["git", "show", f"{since}:{rel}"],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        before = (
            {s.key: s.body for s in sections(rel, old.stdout)}
            if old.returncode == 0
            else {}
        )
        for s in sections(rel, doc.read_text(encoding="utf-8")):
            if before.get(s.key) != s.body:
                changed.add(s.key)
    return changed
