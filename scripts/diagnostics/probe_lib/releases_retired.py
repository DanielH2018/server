"""Release records for services the inventory no longer declares.

A retired role's record outlives it: `<service>.json` stays in the release directory after the
PR that deleted the role deploys. `compute_stale` judges a record against its role's paths, and
the deletion commit changes every one of them, so the record reads stale for good. That parks
`Release Staleness Drift` DOWN with no deploy tag able to clear it, because no tag deploys the
retired service.

Every `manifests_service` is a declared deploy tag, so a record whose service no
`containers_list` entry names belongs to a retired service. Dropping it is the way out;
removing the file from the host is housekeeping, not a requirement.

A service whose entry carries `owner` outlives its record the same way while it is still
declared: Flux applies it, or it is mid-handback, so no Ansible deploy writes it a newer
record. `probe.py releases --stale-only` and `probe.py health` read that through `tree_owners`
and ignore the record (D2.4b, docs/target-architecture/deploy/d2-first-leaf.md).
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


def tree_owners(host_vars=None):
    """Each deploy tag whose `containers_list` entry carries `owner`, to that value.

    `host_vars` is the k8s host's host_vars file, the repo's own when None.

    Raises:
        ValueError: an entry's `owner` is outside `lib.k8s_roles.OWNER_VALUES`.
    """
    # Deferred for the reason `role_dir_names` gives.
    from lib.k8s_roles import tag_owners

    return tag_owners() if host_vars is None else tag_owners(host_vars)


def drop_owned(records, host_vars=None):
    """`records` less those whose service carries `owner`, and the `tree_owners` map read.

    A record carrying `error` is kept, as `drop_retired` keeps it.
    """
    owners = tree_owners(host_vars)
    return [
        r for r in records if "error" in r or r.get("service") not in owners
    ], owners


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
