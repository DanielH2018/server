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


def drop_retired(records, declared):
    """`records` minus those whose service is not in `declared`.

    A record carrying `error` (unreadable or truncated) has no trustworthy service name, so it
    is kept for the caller to report rather than silently dropped.
    """
    return [r for r in records if "error" in r or r.get("service") in declared]


def _is_leftover_dir(role_dir):
    """Whether `role_dir` is a retired role's debris rather than a role.

    Retiring a role removes its tracked files; a gitignored `__pycache__/` left by a pytest
    run keeps the directory itself on disk. Such a shell has no `containers_list` entry, so
    `split_shared_roles` would classify it SHARED and widen every service's staleness paths
    by a role that no longer exists.

    `ansible/filter_plugins/k8s_autodeploy.py:is_leftover_dir` is the authoritative copy of
    this predicate -- it is the one that gates the deployer's config write. This is a second
    derivation rather than an import because that module imports `ansible.errors`, which
    `probe.py` must not need. Keep the two in step; an empty directory is deliberately NOT
    leftover in either.
    """
    found_debris = False
    for path in role_dir.rglob("*"):
        if path.is_dir():
            continue
        if path.suffix != ".pyc" and "__pycache__" not in path.parts:
            return False
        found_debris = True
    return found_debris


def role_dir_names(k8s_roles_dir):
    """Every real role directory name under `k8s_roles_dir`, debris excluded.

    The census `shared_k8s_roles` subtracts the declared deploy tags from. A leftover
    directory has no `containers_list` entry, so counting it would classify it SHARED and
    widen every service's staleness paths by a role that no longer exists. A `k8s_roles_dir`
    that does not exist yields no names, which leaves that caller with nothing to widen.
    """
    if not k8s_roles_dir.is_dir():
        return set()
    return {
        p.name
        for p in k8s_roles_dir.iterdir()
        if p.is_dir() and not _is_leftover_dir(p)
    }
