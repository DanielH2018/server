"""Release records for services the inventory no longer declares.

A retired role's record outlives it: `<service>.json` stays in the release directory after the
PR that deleted the role deploys. `compute_stale` judges a record against its role's paths, and
the deletion commit changes every one of them, so the record reads stale for good. That parks
`Release Staleness Drift` DOWN with no deploy tag able to clear it, because no tag deploys the
retired service.

Every `manifests_service` is a declared deploy tag, so a record whose service no
`containers_list` entry names belongs to a retired service. Dropping it is the way out;
removing the file from the host is housekeeping, not a requirement.
"""

# `probe_lib` is a namespace package under `scripts/`, so reaching `lib.k8s_roles` by package
# name needs `scripts/` on sys.path, as in releases_consumers.py.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))


def drop_retired(records, declared):
    """`records` minus those whose service is not in `declared`.

    A record carrying `error` (unreadable or truncated) has no trustworthy service name, so it
    is kept for the caller to report rather than silently dropped.
    """
    return [r for r in records if "error" in r or r.get("service") in declared]


def role_dir_names(k8s_roles_dir):
    """Every real role directory name under `k8s_roles_dir`, debris excluded.

    The census `shared_k8s_roles` subtracts the declared deploy tags from. A leftover
    directory has no `containers_list` entry, so counting it would classify it SHARED and
    widen every service's staleness paths by a role that no longer exists. A `k8s_roles_dir`
    that does not exist yields no names, which leaves that caller with nothing to widen.
    """
    if not k8s_roles_dir.is_dir():
        return set()
    # Deferred: `lib.k8s_roles` costs PyYAML, Jinja2 and, once it walks, `ansible.errors`,
    # which only the staleness check pays, not every `probe.py` subcommand.
    from lib.k8s_roles import role_dirs

    return {p.name for p in role_dirs(k8s_roles_dir)}
