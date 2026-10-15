"""`promote.py prune`: delete the artifact versions no promotion or rollback can reach.

`artifact_retention.py` chooses the versions, and its docstring states the rule. This module
reads the facts the rule needs, deletes the chosen versions one by one, every signature after
the versions it could sign, then runs `cosign verify` on `prod`'s digest, and on `last-good`'s
when that tag exists. It prints one line per version deleted, then a summary line. Any failed
read deletes nothing.

It reaches GitHub and GHCR only through `forge.py`, as `promote.py` does
(scripts/deploy_tools/tests/test_promote_forge_boundary.py). `promote.py` hands it the two
reads it shares with `decide`: `prod`'s commit and the checkout's ancestry answer.

Run: uv run python scripts/deploy_tools/promote.py prune
"""

import re
import sys
from collections.abc import Callable
from datetime import datetime

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
# `forge` and `artifact_retention` import bare, from scripts/deploy_tools.
_sys.path.insert(1, str(_Path(__file__).resolve().parents[1]))
from lib.exit_codes import FAILED, OK

import forge
from artifact_retention import (
    LAST_GOOD_TAG,
    PROD_TAG,
    Failed,
    PruneFacts,
    Version,
    ancestry_needed,
    select_prunable,
    subjects_needed,
)

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")

ProdCommit = Callable[[dict | None], str | None]
IsAncestor = Callable[[str, str], bool]


class ReadError(RuntimeError):
    """A read outside the forge failed: the checkout's contexts file, or a git command."""


# Both mean "this fact could not be read". `promote.py` raises this same ReadError, so a
# failed ancestry read reaches `_read` as a fact that failed rather than as a crash.
READ_ERRORS = (ReadError, forge.ForgeError)


def parse_version(entry: dict) -> Version:
    """One Packages API version entry as a `Version`.

    Raises:
        ReadError: the entry lacks an integer id, a sha256 digest, a list of string tags or
            a `created_at` with a time zone.
    """
    try:
        tags = entry["metadata"]["container"]["tags"]
        version = Version(
            id=entry["id"],
            digest=entry["name"],
            tags=frozenset(tags),
            created=datetime.fromisoformat(entry["created_at"]),
        )
    except (KeyError, TypeError, ValueError) as err:
        raise ReadError(f"package version {entry!r}: {err!r}") from err
    if (
        not isinstance(version.id, int)
        or not isinstance(version.digest, str)
        or not _DIGEST.fullmatch(version.digest)
        or not isinstance(tags, list)
        or not all(isinstance(tag, str) for tag in tags)
        or version.created.tzinfo is None
    ):
        raise ReadError(f"package version is malformed: {entry!r}")
    return version


def manifest_subject(digest: str, manifest: dict | None) -> str | None:
    """The digest the manifest's `subject` names, or None when it has no `subject`.

    Raises:
        ReadError: the registry has no manifest for a listed version, or its `subject`
            carries no sha256 digest.
    """
    if manifest is None:
        raise ReadError(f"the registry has no manifest for the listed version {digest}")
    subject = manifest.get("subject")
    if subject is None:
        return None
    signed = subject.get("digest") if isinstance(subject, dict) else None
    if not isinstance(signed, str) or not _DIGEST.fullmatch(signed):
        raise ReadError(f"manifest {digest} has a subject with no digest: {subject!r}")
    return signed


def _read[T](read: Callable[[], T]) -> T | Failed:
    """What `read` returns, or `Failed` when it raises one of READ_ERRORS."""
    try:
        return read()
    except READ_ERRORS as err:
        return Failed(str(err))


def _prod(f: forge.Forge, prod_commit: ProdCommit) -> tuple[str, str] | None:
    commit = prod_commit(f.registry.manifest(PROD_TAG))
    if commit is None:
        return None
    digest = f.registry.digest(PROD_TAG)
    if digest is None:
        raise ReadError(
            f"{PROD_TAG} disappeared between its manifest read and its HEAD"
        )
    return commit, digest


def prune_facts(
    f: forge.Forge, now: datetime, prod_commit: ProdCommit, is_ancestor: IsAncestor
) -> PruneFacts:
    """Read every fact `select_prunable` needs, stopping at the first read that fails."""
    versions = _read(
        lambda: [parse_version(entry) for entry in f.github.package_versions()]
    )
    prod = _read(lambda: _prod(f, prod_commit))
    last_good = _read(lambda: f.registry.digest(LAST_GOOD_TAG))
    subjects: dict[str, str | None | Failed] = {}
    ancestry: dict[str, bool | Failed] = {}
    if (
        isinstance(versions, Failed)
        or isinstance(prod, Failed)
        or isinstance(last_good, Failed)
        or prod is None
    ):
        return PruneFacts(versions, prod, last_good, ancestry, subjects, now)
    commit = prod[0]
    for digest in sorted(subjects_needed(versions, now)):
        subjects[digest] = _read(
            lambda d=digest: manifest_subject(d, f.registry.manifest(d))
        )
        if isinstance(subjects[digest], Failed):
            break
    for candidate in sorted(ancestry_needed(versions, now)):
        ancestry[candidate] = _read(lambda c=candidate: is_ancestor(c, commit))
        if isinstance(ancestry[candidate], Failed):
            break
    return PruneFacts(versions, prod, last_good, ancestry, subjects, now)


def prune(
    f: forge.Forge, now: datetime, prod_commit: ProdCommit, is_ancestor: IsAncestor
) -> int:
    """Select, delete one by one, then verify `prod` and `last-good`. Returns the exit code.

    Args:
        f: the forge clients.
        now: the time the retention rule measures age from.
        prod_commit: the commit a `prod` manifest names, as `promote.prod_commit` reads it.
        is_ancestor: whether the first commit is an ancestor of the second, answered by
            the checkout that `promote.py prune --repo` names.
    """
    facts = prune_facts(f, now, prod_commit, is_ancestor)
    selection = select_prunable(facts)
    ok = not selection.failed
    if selection.reason:
        print(f"promote.py: prune: {selection.reason}", file=sys.stderr)

    deleted = 0
    for version, why in selection.delete:
        tags = ",".join(sorted(version.tags)) or "untagged"
        try:
            gone = f.github.delete_package_version(version.id)
        except forge.ForgeError as err:
            # A permission error fails every delete alike, so the first one ends the loop.
            print(f"promote.py: prune: {err}", file=sys.stderr)
            ok = False
            break
        deleted += gone
        state = "deleted" if gone else "already gone"
        print(f"{state} {version.id} {version.digest} {tags}: {why}")

    verified = []
    prod_digest = facts.prod[1] if isinstance(facts.prod, tuple) else None
    last_good = facts.last_good if isinstance(facts.last_good, str) else None
    for tag, digest in ((PROD_TAG, prod_digest), (LAST_GOOD_TAG, last_good)):
        if digest is None:
            continue
        try:
            f.cosign.verify(digest)
            verified.append(tag)
        except forge.ForgeError as err:
            print(f"promote.py: prune: {tag} does not verify: {err}", file=sys.stderr)
            ok = False

    listed = facts.versions if isinstance(facts.versions, list) else []
    # Open question 8 reads these: whether GHCR lists cosign's bundles as package versions.
    # `untagged` answers from the first run; a subject is read only once a version is old.
    untagged = sum(not v.tags for v in listed)
    signed = sum(isinstance(subject, str) for subject in facts.subjects.values())
    print(
        f"listed={len(listed)} untagged={untagged} untagged_with_subject={signed} "
        f"selected={len(selection.delete)} deleted={deleted} "
        f"verified={','.join(verified) or 'none'}"
    )
    return OK if ok else FAILED
