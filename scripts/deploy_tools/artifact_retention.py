"""Choose which versions of the manifest artifact `promote.py prune` deletes.

`select_prunable` is a pure function. `promote.py prune` reads every fact it takes, the
package listing, `prod`'s commit and digest, `last-good`'s digest, one ancestry answer per
`sha-<C>` tag and the `subject` of each old untagged manifest, and hands them over as values,
None for absent, or `Failed`. This module reads nothing itself.

THE RULE. A version is deleted only when it is older than RETENTION_DAYS and is one of:
  - a version whose only tag is `sha-<C>`, where C is a strict ancestor of `prod`'s commit,
    so no promotion can choose it again;
  - an untagged version whose manifest has no `subject`, such as a `verify-drill` bundle that a
    later drill replaced;
  - a signature of a digest deleted in the same run or missing from the listing: the untagged
    cosign bundle whose `subject` is that digest, or the index tagged `sha256-<hex>` that
    cosign writes on a registry without the referrers API.
Everything else stays, however old: the versions tagged `prod`, `last-good`, `held` and
`verify-drill`, every walk candidate, because each descends from `prod`'s commit, and every
signature of a surviving digest. When `prod` is absent, or any fact the selection needs
failed, nothing is deleted. A listing that lacks `prod`'s or `last-good`'s version counts as
failed, because a short listing would make their signatures look orphaned.

The plan is docs/target-architecture/deploy/d1-flux-installed.md, *Decisions*, *Old artifact
versions are pruned daily (D1.7)*.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

PROD_TAG = "prod"
LAST_GOOD_TAG = "last-good"
# The revision a failed promotion left as `prod` under a hold without a reset (H1.1b). The
# in-flight wait reads it, so a cleared hold of that kind does not read as a run in flight.
HELD_TAG = "held"

# How old a version must be before `prune` may delete it. Every deletion, a signature's
# included, waits this long, so a version pushed while a run lists the package is never one.
RETENTION_DAYS = 14
_SHA_TAG = re.compile(r"sha-([0-9a-f]{40})")
# cosign's referrers fallback tags an index with the signed digest, its colon made a dash.
_SIGNATURE_INDEX_TAG = re.compile(r"sha256-([0-9a-f]{64})")


@dataclass(frozen=True)
class Failed:
    """A read that `prune` could not make. Any one of them means nothing is deleted.

    Attributes:
        reason: what failed, for the log.
    """

    reason: str


@dataclass(frozen=True)
class Version:
    """One version of the artifact's package, as the Packages API lists it.

    Attributes:
        id: the Packages API's id, which the delete takes.
        digest: the manifest digest, which the API calls the version's `name`.
        tags: every tag the version carries.
        created: when the version was pushed. A retag moves `updated_at`, never this.
    """

    id: int
    digest: str
    tags: frozenset[str]
    created: datetime


@dataclass(frozen=True)
class PruneFacts:
    """Every fact the selection reads, each a value, None for absent, or `Failed`.

    Attributes:
        versions: the whole package listing.
        prod: `prod`'s commit and digest, or None when the tag does not exist.
        last_good: `last-good`'s digest, or None when the tag does not exist.
        ancestry: for each commit C that `ancestry_needed` names, whether C is an ancestor
            of `prod`'s commit or equal to it.
        subjects: for each digest that `subjects_needed` names, the digest its manifest's
            `subject` names, or None when it has no `subject`.
        now: the time each version's age is measured from.
    """

    versions: list[Version] | Failed
    prod: tuple[str, str] | None | Failed
    last_good: str | None | Failed
    ancestry: Mapping[str, bool | Failed]
    subjects: Mapping[str, str | None | Failed]
    now: datetime


@dataclass(frozen=True)
class Selection:
    """What `select_prunable` chose.

    Attributes:
        delete: each version to delete with the rule that selected it. Every signature
            comes after every version it could sign, so a run stopped partway leaves only
            orphaned signatures, which the next run selects.
        reason: why nothing was selected, when a read failed or `prod` is absent.
        failed: whether a read failed.
    """

    delete: tuple[tuple[Version, str], ...] = ()
    reason: str = ""
    failed: bool = False


def _old(version: Version, now: datetime) -> bool:
    return now - version.created > timedelta(days=RETENTION_DAYS)


def _only_tag(version: Version, pattern: re.Pattern) -> str | None:
    """What `pattern` captures from the version's tag, when it has exactly one."""
    if len(version.tags) != 1:
        return None
    match = pattern.fullmatch(next(iter(version.tags)))
    return match.group(1) if match else None


def ancestry_needed(versions: list[Version], now: datetime) -> set[str]:
    """Each commit C whose `sha-<C>` version is old enough to prune and has no other tag."""
    return {
        commit
        for v in versions
        if _old(v, now) and (commit := _only_tag(v, _SHA_TAG)) is not None
    }


def subjects_needed(versions: list[Version], now: datetime) -> set[str]:
    """The digest of each untagged version old enough to prune."""
    return {v.digest for v in versions if _old(v, now) and not v.tags}


def select_prunable(facts: PruneFacts) -> Selection:
    """Choose the versions to delete under THE RULE in this module's docstring.

    Pure: every fact arrives in `facts`. Any `Failed` among the facts the selection needs,
    or a needed answer that is missing, selects nothing.
    """
    versions, prod, last_good = facts.versions, facts.prod, facts.last_good
    if isinstance(versions, Failed):
        return Selection(reason=versions.reason, failed=True)
    if isinstance(prod, Failed):
        return Selection(reason=prod.reason, failed=True)
    if isinstance(last_good, Failed):
        return Selection(reason=last_good.reason, failed=True)
    if prod is None:
        return Selection(reason=f"{PROD_TAG} does not exist, so nothing is pruned")
    prod_commit, prod_digest = prod
    # A listing that missed `prod`'s version, say a page short, would make prod's signatures
    # look orphaned. The registry's own answer for each tag must appear in the listing.
    for tag, digest in ((PROD_TAG, prod_digest), (LAST_GOOD_TAG, last_good)):
        if digest and not any(v.digest == digest and tag in v.tags for v in versions):
            reason = f"the listing has no version {digest} tagged {tag}"
            return Selection(reason=reason, failed=True)

    now = facts.now
    answers: dict[tuple[str, str], str | bool | None] = {}
    for key, needed, read in (
        ("subject", subjects_needed(versions, now), facts.subjects),
        ("ancestry", ancestry_needed(versions, now), facts.ancestry),
    ):
        for name in needed:
            answer = read.get(name, Failed(f"no {key} read for {name}"))
            if isinstance(answer, Failed):
                return Selection(reason=answer.reason, failed=True)
            answers[key, name] = answer

    oldest_first = sorted(versions, key=lambda v: v.created)
    first = []
    for v in oldest_first:
        commit = _only_tag(v, _SHA_TAG)
        if answers.get(("ancestry", commit)) and commit != prod_commit:
            first.append((v, f"sha-{commit} is a strict ancestor of prod's commit"))
        elif ("subject", v.digest) in answers and answers["subject", v.digest] is None:
            first.append((v, "untagged, with no subject"))
    surviving = {v.digest for v in versions} - {v.digest for v, _ in first}
    signatures = []
    for v in oldest_first:
        signed = answers.get(("subject", v.digest))
        if _old(v, now) and (index := _only_tag(v, _SIGNATURE_INDEX_TAG)):
            signed = f"sha256:{index}"
        if signed is not None and signed not in surviving:
            signatures.append((v, f"signs {signed}, which is not kept"))
    return Selection(delete=tuple(first + signatures))
