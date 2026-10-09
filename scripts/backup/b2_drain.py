#!/usr/bin/env python3
"""Delete a stranded Longhorn backup prefix directly through the B2 API.

WHY THIS EXISTS. Deleting a backup chain through Longhorn costs on the order of hundreds of
Class C transactions, because a prune walks the whole block tree once per deleted backup, and
B2's free tier allows 2,500 Class C a day. Doing it through the API costs one listing for the
whole store (5 Class C) plus deletes, which are Class A and unmetered. Scoping:
docs/archive/b2-api-drain-scoping.md.

WHY IT IS SAFE TO DELETE A WHOLE PREFIX. Longhorn namespaces blocks under each volume's own
prefix — `volumes/<xx>/<yy>/<volume>/blocks/<aa>/<bb>/<sha256>.blk`. The same content hash
appears as a separate object under every volume that uses it, so deduplication is within a
volume and never across volumes. Removing one volume's prefix cannot take a block another
volume's backup depends on.

WHAT STOPS IT DELETING SOMETHING LIVE. Three things, in order:
  - The caller passes the live Longhorn volume list. An empty or unreadable list refuses
    everything rather than classifying everything as strandable — the same fail-closed shape as
    the migrated-chain mode of ansible/prune_backups.yml, which learned it from a padded-column
    bug that made every backup look like an orphan.
  - Any requested volume that still exists in that list is refused by name.
  - Nothing is deleted without --apply. Discovery proposes; the operator disposes.

WHY IT LISTS VERSIONS AND NOT NAMES. B2 keeps superseded versions, and a delete writes a hide
marker over the upload version rather than removing it. b2_list_file_names would therefore
under-report what has to go, and reading `action == "upload"` as "still live" makes a finished
deletion look like a no-op. Current state is the FIRST version returned for a name; the rest
are retained history.

Usage (credentials come from the environment; ansible/prune_backups.yml -e prune_mode=b2-drain
supplies them):
    B2_KEY_ID=... B2_APP_KEY=... uv run python scripts/backup/b2_drain.py \
        --live-volumes-file /tmp/live.txt --volumes pvc-aaa,pvc-bbb [--apply]
"""

import argparse
import os
import sys
from collections import defaultdict
from pathlib import Path as _Path

sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # scripts/
from lib.b2 import B2Error, B2Session

BACKUPSTORE_PREFIX = "longhorn/backupstore/volumes/"
# A listing still unfinished after this many pages (200,000 versions) is refused rather than
# acted on: a drain that deleted from a partial view could not verify it had finished.
MAX_PAGES = 200


class DrainError(Exception):
    """Anything that should stop the run with a message rather than a traceback."""


def volume_of(key: str, prefix: str = BACKUPSTORE_PREFIX) -> str | None:
    """The volume name a backupstore key belongs to, or None if it is not under a volume.

    Keys look like `<prefix><xx>/<yy>/<volume>/...`, so the volume is the third segment. A key
    with fewer segments is a stray at the top of the store and is deliberately not attributed to
    any volume — attributing it by guess is how a drain reaches outside its prefix.
    """
    if not key.startswith(prefix):
        return None
    parts = key[len(prefix) :].split("/")
    if len(parts) < 4:
        return None
    return parts[2]


def parse_volume_list(raw: str) -> list[str]:
    """Volume names from either a comma-separated argument or a file, order preserved.

    Both separators are accepted so the same parser handles `--volumes a,b` and a file written
    one-per-line; duplicates are dropped so a name listed twice is not deleted twice.
    """
    seen, out = set(), []
    for chunk in raw.replace(",", "\n").splitlines():
        name = chunk.strip()
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def volume_prefix(key: str, prefix: str = BACKUPSTORE_PREFIX) -> str:
    """The `<prefix><xx>/<yy>/<volume>/` a key sits under.

    Longhorn shards volumes into two hash directories whose names cannot be derived from the
    volume name here, so the prefix is read back off a key the listing already returned. That
    makes the verification re-list cost one Class C instead of re-walking the whole store.
    """
    parts = key[len(prefix) :].split("/")
    return prefix + "/".join(parts[:3]) + "/"


def current_versions(versions: list[dict]) -> dict[str, dict]:
    """Map each file name to its CURRENT version, given versions newest-first per name.

    b2_list_file_versions returns a name's versions newest first. The first one seen is therefore
    the current state: `action == "hide"` means the file is already deleted and everything after
    it is retained history.
    """
    current: dict[str, dict] = {}
    for version in versions:
        current.setdefault(version["fileName"], version)
    return current


def live_object_count(versions: list[dict]) -> int:
    """How many files under these versions actually still exist."""
    return sum(
        1 for v in current_versions(versions).values() if v.get("action") == "upload"
    )


def classify(
    requested: list[str], present: set[str], live: set[str]
) -> tuple[list[str], dict[str, str]]:
    """Split the requested volumes into (drainable, {refused: reason}).

    `live` is the set of Longhorn volumes that still exist. A volume in it is somebody's current
    backup chain, never debris.
    """
    if not live:
        raise DrainError(
            "the live-volume list is empty, so nothing can be proven stranded. Refusing "
            "everything rather than treating every prefix as an orphan."
        )
    drainable, refused = [], {}
    for name in requested:
        if name in live:
            refused[name] = (
                "its Longhorn volume still exists — this is a live backup chain"
            )
        elif name not in present:
            refused[name] = "no such prefix in the backupstore"
        else:
            drainable.append(name)
    return drainable, refused


def read_live_volumes(path: str) -> set[str]:
    try:
        with open(path, encoding="utf-8") as handle:
            return {line.strip() for line in handle if line.strip()}
    except OSError as exc:
        raise DrainError(f"cannot read the live-volume list at {path}: {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    """Parse args, drain the requested volumes' B2 prefixes, and report the result.

    Without ``--apply`` this only reports what a drain would delete. Exits 2 on a
    missing credential, an unreadable input file, no volumes named, or a ``DrainError``
    (including a failed post-delete verification) or a B2 API failure; exits 1 if any
    requested volume was refused (e.g. still live); exits 0 otherwise.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-volumes-file", required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--volumes",
        help="comma-separated Longhorn volume names whose prefixes to drain",
    )
    # Twenty volume names is 820 characters, which no shell pastes reliably on one line. The
    # allow-list is still explicit — the operator names and can read the file — only the way it
    # arrives changes.
    source.add_argument(
        "--volumes-file",
        help="file of volume names, one per line or comma-separated",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually delete; without it the run only reports what it would do",
    )
    args = parser.parse_args(argv)

    try:
        key_id, app_key = os.environ["B2_KEY_ID"], os.environ["B2_APP_KEY"]
    except KeyError as exc:
        print(f"missing credential in the environment: {exc}", file=sys.stderr)
        return 2

    if args.volumes_file:
        try:
            with open(args.volumes_file, encoding="utf-8") as handle:
                raw = handle.read()
        except OSError as exc:
            print(f"cannot read {args.volumes_file}: {exc}", file=sys.stderr)
            return 2
    else:
        raw = args.volumes
    requested = parse_volume_list(raw)
    if not requested:
        print("no volumes were named", file=sys.stderr)
        return 2

    try:
        live = read_live_volumes(args.live_volumes_file)
        b2 = B2Session(key_id, app_key)
        if not b2.bucket_id:
            raise DrainError(
                "the application key is not bucket-scoped, so there is no bucket to drain"
            )
        if "deleteFiles" not in b2.capabilities:
            raise DrainError(
                "the application key has no deleteFiles capability, so a drain would report "
                "success while deleting nothing"
            )

        everything = b2.list_files(
            BACKUPSTORE_PREFIX, versions=True, max_pages=MAX_PAGES
        )
        by_volume: dict[str, list[dict]] = defaultdict(list)
        for version in everything:
            name = volume_of(version["fileName"])
            if name:
                by_volume[name].append(version)

        drainable, refused = classify(requested, set(by_volume), live)
        for name, reason in refused.items():
            print(f"REFUSED {name}: {reason}")

        for name in drainable:
            versions = by_volume[name]
            live_now = live_object_count(versions)
            print(
                f"{'DRAIN' if args.apply else 'WOULD DRAIN'} {name}: "
                f"{len(versions)} versions, {live_now} live objects"
            )
            if not args.apply:
                continue
            own_prefix = volume_prefix(versions[0]["fileName"])
            for version in versions:
                b2.delete(version["fileName"], version["fileId"])
            # Verify by re-reading rather than by counting our own deletes: a drain that trusts
            # its own tally already reported "1,676/1,676 deleted" here while leaving five
            # volumes over retention.
            still = b2.list_files(own_prefix, versions=True, max_pages=MAX_PAGES)
            if still:
                raise DrainError(
                    f"{name} still holds {len(still)} versions after the drain; stopping "
                    "before touching anything else"
                )
            print(f"  verified empty: {name}")

        print(f"list calls (Class C): {b2.list_calls}")
        if drainable and not args.apply:
            print("dry run — nothing was deleted. Re-run with --apply.")
        return 1 if refused else 0
    except (DrainError, B2Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
