"""Release records for services the inventory no longer declares.

A retired role's record outlives it: `<service>.json` stays in the release directory after the
PR that deleted the role deploys. `compute_stale` judges a record against its role's paths, and
the deletion commit changes every one of them, so the record reads stale for good. That parks
`Release Staleness Drift` DOWN with no deploy tag able to clear it, because no tag deploys the
retired service. #2813's game-stats merge retired terraria-stats and valheim-stats this way.

Every `manifests_service` is a declared deploy tag, so a record whose service no
`containers_list` entry names belongs to a retired service. Dropping it is the way out;
removing the file from the host is housekeeping, not a requirement.
"""


def drop_retired(records, declared):
    """`records` minus those whose service is not in `declared`.

    A record carrying `error` (unreadable or truncated) has no trustworthy service name, so it
    is kept for the caller to report rather than silently dropped.
    """
    return [r for r in records if "error" in r or r.get("service") in declared]
