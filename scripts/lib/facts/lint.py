"""Lint for the fact stores: the forms the spec excludes, and the atoms that do not resolve.

Two severities. An error is a citation that cannot be support (a rejected form, an atom that
does not resolve, an ambiguous marker), a support record someone edited by hand, or a document
shape the section parser reads as something the author did not mean (two sections under one
heading text, a fence with no closing marker), or prose naming a host the inventory no longer
holds. A warning is a habit the spec asks writers to
drop but a machine cannot judge (a number that may or may not be a count, a test with no
backref). ``fact_status.py lint`` exits non-zero on errors only.
"""

import os
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from lib.ansible_inventory import host_names
from lib.git import git

from .atoms import Ambiguous, backrefs, hash_atom
from .citations import (
    HISTORY_MARKER,
    in_tree,
    mask_history,
    parse_citations,
    repo_docs,
    sections,
    spans,
    tracked_files,
)
from .lock import LOCK_REL, lock_tampered
from .pins import image_pins, pinned_keys, split_image_ref
from .removed import identifiers, removed_tokens, still_present

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
        "duplicate-heading",
        "unbalanced-fence",
        "renovate-pin",
        "retired-host",
        "version-as-fact",
        "vanished-identifier",
    }
)
WARN_RULES = frozenset({"count-as-fact", "one-way-test", "version-as-fact"})

_FENCE_MARK = re.compile(r"```")

_COUNT = re.compile(
    r"\b(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\s+"
    r"(?:entries|memories|roles|workloads|services|tests|monitors|files|hooks|checks)\b",
    re.I,
)
_DATE_CLAIM = re.compile(r"\bverified\b[^.\n]{0,40}\b\d{4}-\d{2}-\d{2}\b", re.I)

INVENTORY_REL = "ansible/inventory/hosts.ini"


def host_pattern(hosts: frozenset[str]) -> re.Pattern | None:
    """A token shaped like one of ``hosts``: their shared ``<prefix>-`` and one more word.

    The prefix is derived, never listed, so a host added to the inventory widens it. The
    boundaries keep out identifiers that merely contain a host name. A token preceded by a
    word character, ``/``, ``.`` or ``-`` is inside a path or a longer name
    (``/srv/artifacts/daniel-box-claude``, ``host_vars/daniel-pi.yml``). One followed by a
    word character, ``-`` or ``.<word>`` is a longer name or a domain (``daniel-hunter.com``).
    A sentence-final period still ends a host mention. None when the hosts share no prefix.
    """
    common = os.path.commonprefix(sorted(hosts))
    prefix = common[: common.rfind("-") + 1]
    if not prefix:
        return None
    return re.compile(rf"(?<![\w./-]){re.escape(prefix)}[a-z0-9]+(?![\w-]|\.\w)")


@dataclass(frozen=True)
class LintFinding:
    unit: str
    rule: str
    detail: str
    warn: bool

    def __post_init__(self) -> None:
        # `_f` reads WARN_RULES to set `warn`, so a rule missing from RULES would also be
        # missing from WARN_RULES and silently grade as an error.
        if self.rule not in RULES:
            raise ValueError(f"{self.rule!r} is not in RULES")


def _f(unit: str, rule: str, detail: str) -> LintFinding:
    return LintFinding(unit, rule, detail, rule in WARN_RULES)


def lint_sections(
    repo: Path, unit_keys: set[str] | None, since: str | None = None
) -> list[LintFinding]:
    """Every ``RULES`` finding in the named sections, or in every section when ``unit_keys`` is None.

    Errors and warnings come back in one list; the caller splits them on ``LintFinding.warn``.
    Unlike ``lock.check_lock`` this reads no lock row: a never-verified section is linted the
    same as a verified one, which is what makes ``lint --changed-since`` a ratchet.

    ``since`` adds ``vanished-identifier``, which needs a range to read. Its findings land in
    whichever section names the vanished token, inside ``unit_keys`` or not: the commit that
    removes an identifier is usually a code-only commit that edits no section at all.
    """
    out: list[LintFinding] = []
    tracked = tracked_files(repo)
    pins: dict[str, frozenset[str]] = {}
    hosts = frozenset(host_names(repo / INVENTORY_REL))
    host_rx = host_pattern(hosts)
    images = image_pins(repo, tracked)

    def _pins(rel: str) -> frozenset[str]:
        if rel not in pins:
            pins[rel] = pinned_keys(repo, rel)
        return pins[rel]

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
        secs = sections(rel, doc.read_text(encoding="utf-8"))
        # Two sections with one heading text share one key, so the lock holds one row for
        # both and every keyed reader (`lock.repo_citations`, `changed_units`) keeps the
        # last one: the first section's atoms are never graded. Nothing downstream can
        # tell the two apart, so the document is what has to change.
        for key, n in Counter(s.key for s in secs).items():
            if n > 1 and (unit_keys is None or key in unit_keys):
                out.append(
                    _f(
                        key,
                        "duplicate-heading",
                        f"{n} sections in {rel} carry this heading and collapse onto one lock row; rename one",
                    )
                )
        for sec in secs:
            if unit_keys is not None and sec.key not in unit_keys:
                continue
            # `citations._FENCE` pairs markers left to right, so an unclosed fence leaves its
            # stray marker unmasked and everything after it live: a `#` line inside it
            # becomes a heading and an example citation becomes support. A balanced
            # document leaves an even count in every section body, so the section holding
            # the odd one is where the stray marker is.
            if len(_FENCE_MARK.findall(sec.body)) % 2:
                out.append(
                    _f(
                        sec.key,
                        "unbalanced-fence",
                        "an unclosed ``` fence; the text after it is read as prose, not as an example",
                    )
                )
            cites, rejects = parse_citations(sec.body)
            # Out-of-tree spans are prose, not broken support: `origin/master` and an
            # untracked or gitignored file parse as paths and name nothing this checkout
            # tracks. A REJECTED form is not filtered — a line number is a claim about this
            # tree whatever its prefix — so the rejects list below is the unfiltered one.
            cites = [c for c in cites if in_tree(c, tracked)]
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
                elif c.form == "yaml" and c.selector.split(".")[0] in _pins(c.path):
                    out.append(
                        _f(
                            sec.key,
                            "renovate-pin",
                            f"`{c.raw}` is a value Renovate bumps; cite `{c.path}` and name the key in prose",
                        )
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
            prose = mask_history(sec.body)
            named_hosts = set(host_rx.findall(prose)) if host_rx else set()
            out.extend(
                _f(
                    sec.key,
                    "retired-host",
                    f"`{name}` is not a host in {INVENTORY_REL}; rewrite the sentence, "
                    f"or open its paragraph or bullet with `{HISTORY_MARKER}`",
                )
                for name in sorted(named_hosts - hosts)
            )
            out.extend(_version_facts(sec.key, rel, prose, images))
    if since is not None:
        out.extend(vanished_identifiers(repo, since, tracked))
    return out


# A generated block restates its source on every regeneration, so a pin it names is never stale.
_GENERATED = re.compile(r"<!-- generated_from:.*?<!-- /generated_from -->", re.S)


def _version_facts(
    key: str,
    doc_rel: str,
    prose: str,
    images: dict[str, tuple[tuple[str, str, str], ...]],
) -> list[LintFinding]:
    """A ``version-as-fact`` warning per backticked ``image:tag`` span naming a pinned image.

    A warning, never an error: a Renovate bump that makes the span stale cannot edit prose, and
    #4012 ruled that a Renovate PR must not arrive red over documentation. When several pins
    share the image, the span is stale only if its tag matches none of them, and the message
    names the pin nearest the doc. speedtest's upstream `v1.14.7` is out of scope: it is not an
    ``image:tag`` span, and its pin is ``latest@sha256``, which no tag in prose can match.
    """
    doc_dir = doc_rel.rpartition("/")[0] + "/"
    found: list[LintFinding] = []
    for raw in dict.fromkeys(spans(_GENERATED.sub("", prose))):
        parts = split_image_ref(raw)
        if not parts or parts[0] not in images:
            continue
        name, tag = parts
        pins = images[name]
        same = [p for p in pins if p[0] == tag]
        pin_tag, var, path = sorted(
            same or pins, key=lambda p: not p[2].startswith(doc_dir)
        )[0]
        where = (
            f"`{var}` in {path}" if var != "image:" else f"the `image:` line in {path}"
        )
        if same:
            detail = f"`{raw}` restates {where}, a value Renovate moves; name the variable instead"
        else:
            detail = f"`{raw}` is stale: {where} pins `{name}:{pin_tag}`; name the variable instead"
        found.append(_f(key, "version-as-fact", detail))
    return found


# ── vanished-identifier ──────────────────────────────────────────────────────────────────
#
# A commit that removes the last occurrence of an identifier a CLAUDE.md still names leaves a
# stale sentence whatever the lock says. A replay of the 1,026 commits from 2026-09-19 to
# 8181ee59f found six such sentences this way, against at most one catch by the lock.


def vanished_identifiers(
    repo: Path, since: str, tracked: frozenset[str]
) -> list[LintFinding]:
    """A finding per section that names, in backticks, an identifier this branch removed.

    The branch is ``merge-base(since, HEAD)`` to the working tree, so a ``since`` that moved on
    after the branch was cut does not read master's own additions as this branch's removals.
    A token is reported only if it occurs nowhere at HEAD (``still_present``), and only in a
    section that still names it, so a commit that fixes its own sentence stays silent. A
    history paragraph or bullet is skipped, and so is a ``generated_from`` block: its
    generator rewrites it, and the hook that checks those blocks fails until it has.
    """
    base = (
        git("merge-base", since, "HEAD", cwd=repo, check=False).stdout.strip() or since
    )
    removed = removed_tokens(repo, base)
    if not removed:
        return []
    named: dict[str, set[str]] = {}
    for doc in repo_docs(repo):
        rel = doc.relative_to(repo).as_posix()
        for sec in sections(rel, doc.read_text(encoding="utf-8")):
            for span in spans(_GENERATED.sub("", mask_history(sec.body))):
                for tok in identifiers(span) & removed:
                    named.setdefault(tok, set()).add(sec.key)
    held = still_present(repo, sorted(named), tracked)
    return [
        _f(
            key,
            "vanished-identifier",
            f"`{tok}` was removed in {base[:9]}..HEAD or the uncommitted changes and occurs "
            f"nowhere else in the tree; edit the sentence, or open its paragraph or bullet "
            f"with `{HISTORY_MARKER}`",
        )
        for tok in sorted(set(named) - held)
        for key in sorted(named[tok])
    ]


def changed_units(repo: Path, since: str) -> set[str]:
    """The sections whose text differs between ``since`` and the working tree.

    Raises ``ValueError`` when ``since`` names no commit. This is the prek hook's ratchet and
    it runs against ``origin/master``, which a shallow or freshly cloned checkout may not
    have: without the check, ``git show`` fails per document, every ``before`` map comes back
    empty, and every section in the repo reads as changed — the hook then lints the whole
    tree and reports a wall of errors that names nothing the commit touched.
    """
    resolved = git(
        "rev-parse", "--verify", "--quiet", f"{since}^{{commit}}", cwd=repo, check=False
    )
    if resolved.returncode != 0:
        raise ValueError(f"cannot resolve {since!r}")
    changed: set[str] = set()
    for doc in repo_docs(repo):
        rel = doc.relative_to(repo).as_posix()
        before = bodies_at(repo, since, rel)
        for s in sections(rel, doc.read_text(encoding="utf-8")):
            if before.get(s.key) != s.body:
                changed.add(s.key)
    return changed


def bodies_at(repo: Path, since: str, rel: str) -> dict[str, str]:
    """Each section body of the doc ``rel`` as ``since`` holds it; empty when the doc is absent there."""
    old = git("show", f"{since}:{rel}", cwd=repo, check=False)
    if old.returncode != 0:
        return {}
    return {s.key: s.body for s in sections(rel, old.stdout)}


def first_cited_units(repo: Path, since: str, keys: set[str]) -> set[str]:
    """The sections in ``keys`` that cite nothing at ``since``: absent there, or citing no tracked atom.

    The reverify hook records such a section when it cites something now and has no lock row,
    so its first citation lands verified in the commit that adds it. A backlog section that
    already cited an atom at ``since`` is not in this set: a prose edit to it is not the moment
    its citations were written. Citations at ``since`` are judged against today's tracked set.
    """
    tracked = tracked_files(repo)
    out: set[str] = set()
    for rel in sorted({k.partition("#")[0] for k in keys}):
        before = bodies_at(repo, since, rel)
        for key in keys:
            if key.partition("#")[0] != rel:
                continue
            cites, _ = parse_citations(before.get(key, ""))
            if not any(in_tree(c, tracked) for c in cites):
                out.add(key)
    return out
