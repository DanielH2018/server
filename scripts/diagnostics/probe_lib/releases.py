"""`probe.py releases` -- which commit produced the manifests each k8s service is running.

WHY THIS EXISTS. `roles/k8s/manifests/tasks/release_stamp.yml` writes a record per service after
every apply, naming the commit that rendered the bytes. Without a reader that record is a state
nobody can see, which is half a feature. This is the reader.

WHAT IT ANSWERS THAT NOTHING ELSE DOES. `kubectl` reports what is running; git reports what is
committed; neither knows which commit produced the running manifests. `deploy.sh` renders from
whatever tree it is invoked in, so those two can disagree without anything going red -- a
worktree 48 commits behind master reverted claude-otel for nine minutes on 2026-08-19 and the
only symptom was a scrape-target count moving.

THREE FLAGS. `dirty` means the tree had uncommitted tracked changes, so no commit reproduces
those bytes. `unmerged` means the commit is not an ancestor of origin/master -- a service
running code that never landed. `stale` means origin/master has moved past the applied commit
under the service's own role, or one of the shared roles every service's manifests depend on
(`shared_k8s_roles()` -- `manifests`, `rollout-drain`, and the rest of the roles with no
`containers_list` entry of their own). `stale` is what makes a deferred k8s change visible: the
gitops deployer ff-merges a non-auto-deployable k8s role change and pages Discord once, and
every other monitored marker then reads clean while the cluster still runs the old manifests
(issue #947). All three flags are normal mid-slice and alarming a week later, which is why they
are reported rather than judged.

Exit codes: 0 when every record is clean, 1 when any service is dirty, unmerged or stale, 2 when
no records exist at all (nothing has been deployed since the stamp shipped). `--stale-only`
answers the narrower question a cron needs: 0 when nothing is stale and every known k8s service
has a record, 1 otherwise.
"""

import json
import os
import subprocess
from pathlib import Path

# `probe_lib` is a namespace package under `scripts/`, so reaching a sibling by package name
# needs `scripts/` on sys.path — a module gets only its importer's path otherwise, and
# pyproject's `pythonpath` is a pytest setting. This has to sit ABOVE the imports below.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))


# Mirrors manifests_release_dir in roles/k8s/manifests/defaults/main.yml. A mismatch makes this
# reader silently report "no records", so scripts/diagnostics/tests/test_probe_releases.py asserts the
# two agree rather than trusting the comment.
RELEASE_DIR = Path("/var/lib/homelab/k8s-releases.d")

from lib.repo_paths import REPO as REPO_ROOT  # noqa: E402


def _git_env():
    """The environment for a git subprocess that must target `repo_root` and nothing else.

    `GIT_DIR`/`GIT_WORK_TREE`/`GIT_INDEX_FILE` in the environment override `cwd`, so a caller
    running this reader from inside another git operation -- prek's own `pytest` hook runs
    under `git commit`, with exactly these set to that commit's in-progress index -- would
    otherwise point every git call in this module at the WRONG repository, and a real commit in
    `repo_root` reads as "commit unknown to this checkout". `repo_root` is the explicit
    parameter every function below already takes to be scoped and testable; honoring it means
    the ambient environment can't override it.
    """
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def load_records(release_dir=RELEASE_DIR, previous=False):
    """Read every release record in `release_dir`, newest-applied first.

    Pure apart from the filesystem read: returns a list of dicts, skipping anything that does
    not parse. A record that cannot be parsed is reported as such rather than dropped -- a
    truncated write is exactly the case where silence is worst.
    """
    suffix = ".previous.json" if previous else ".json"
    records = []
    if not release_dir.is_dir():
        return records
    for path in sorted(release_dir.glob("*" + suffix)):
        if not previous and path.name.endswith(".previous.json"):
            continue
        try:
            records.append(json.loads(path.read_text()))
        except (OSError, ValueError) as exc:
            records.append({"service": path.stem, "error": str(exc)})
    records.sort(key=lambda r: r.get("applied_at", ""), reverse=True)
    return records


def merged_commits(commits, repo_root=REPO_ROOT):
    """Return the subset of `commits` that are ancestors of origin/master.

    One `git merge-base --is-ancestor` per distinct commit, not per service: a full deploy
    stamps ~54 records that almost always share one commit. An unknown commit (a worktree
    branch that was pruned, a shallow clone) counts as NOT merged, which is the safe reading --
    it means nobody can show where those bytes came from.
    """
    merged = set()
    for commit in {c for c in commits if c}:
        try:
            rc = subprocess.run(
                ["git", "merge-base", "--is-ancestor", commit, "origin/master"],
                cwd=repo_root,
                env=_git_env(),
                capture_output=True,
                timeout=10,
            ).returncode
        except OSError, subprocess.SubprocessError:
            continue
        if rc == 0:
            merged.add(commit)
    return merged


def _deploy_tags():
    """Import `scripts/deploy_tools/deploy_tags` lazily.

    Every other `probe.py` subcommand loads this module through `run_releases`'s import at the
    top of `probe.py`, so a module-level import here would pay `deploy_tags`'s host_vars YAML
    parse on every invocation, not just `releases`. `scripts/` is already on `sys.path` from the
    bootstrap at the top of this file, which is the same directory `deploy_tags.py` itself
    inserts, so the import needs nothing further.
    """
    from deploy_tools import deploy_tags

    return deploy_tags


# Roles under ansible/roles/k8s/ with no containers_list entry -- manifests, rollout-drain and
# the rest render or gate the applied bytes for EVERY k8s service, not just their own. Read at
# call time rather than pinned as a frozenset here: split_shared_roles derives it from the tree
# rather than repeating the SHARED_K8S_ROLES list gitops_deploy/files/deploy_k8s.py already
# maintains, so the two can't drift the way ansible/filter_plugins/k8s_autodeploy.py's own copy
# is pinned to stay in step with (test_denylist_parsers_agree.py).
def shared_k8s_roles(k8s_roles_dir=None, host_vars=None):
    """The k8s role directories no service's own role_paths would otherwise cover.

    Without this widening, a change to `roles/k8s/manifests/` -- the role that renders every
    service's manifests -- reads clean for every one of them, which is the false-GREEN issue
    #947 names: the deployer defers a shared-role change exactly like a per-service one, but
    nothing short of this widening can see it.
    """
    deploy_tags = _deploy_tags()
    k8s_roles_dir = k8s_roles_dir or (REPO_ROOT / "ansible/roles/k8s")
    host_vars = host_vars or deploy_tags.HOST_VARS
    if not k8s_roles_dir.is_dir():
        return frozenset()
    all_dirs = {p.name for p in k8s_roles_dir.iterdir() if p.is_dir()}
    _, shared = deploy_tags.split_shared_roles(all_dirs, host_vars)
    return frozenset(shared)


def role_paths_for(service, shared_roles):
    """The `ansible/roles/k8s/` paths whose history decides whether `service` is stale."""
    return [f"ansible/roles/k8s/{service}/"] + [
        f"ansible/roles/k8s/{r}/" for r in sorted(shared_roles)
    ]


def _is_real_change(path):
    """False for a path that never reaches a deployed manifest: docs and a role's own tests.

    A role's `tests/` directory holds pytest guards over its `files/*.py`, never something
    `k8s/manifests` stages (`ansible/tests/repo/test_no_role_ships_a_test_file.py` enforces
    that tree-wide) -- so a change there cannot make the applied manifests stale.
    """
    if path.endswith(".md"):
        return False
    if "tests" in path.split("/")[:-1]:
        return False
    return True


def _changed_files(commit, paths, repo_root, ref):
    """Real (non-doc, non-test) files under `paths` changed between `commit` and `ref`.

    None means the range could not be resolved -- `commit` is not a rev this checkout knows
    (a pruned worktree branch, a shallow clone) -- which the caller must treat as stale rather
    than silently skip: a commit nobody can find is not evidence the manifests are current.
    """
    try:
        result = subprocess.run(
            [
                "git",
                "log",
                "--name-only",
                "--format=",
                f"{commit}..{ref}",
                "--",
                *paths,
            ],
            cwd=repo_root,
            env=_git_env(),
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
    except OSError, subprocess.SubprocessError:
        return None
    if result.returncode != 0:
        return None
    changed = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    return sorted(p for p in changed if _is_real_change(p))


def compute_stale(records, repo_root=REPO_ROOT, ref="origin/master", shared_roles=None):
    """{service: reason} for every record whose own or shared role paths changed since `ref`.

    One `git log` per distinct commit, not per service -- a full deploy stamps ~54 records
    sharing one commit, and grouping first keeps this from being 54 subprocess calls for what a
    single one already answers for the union of every service's role_paths.
    """
    shared_roles = shared_roles if shared_roles is not None else shared_k8s_roles()
    by_commit = {}
    for rec in records:
        if "error" in rec:
            continue
        commit, service = rec.get("commit"), rec.get("service")
        if not commit or not service:
            continue
        by_commit.setdefault(commit, []).append(service)

    stale = {}
    for commit, services in by_commit.items():
        paths = sorted(
            {p for svc in services for p in role_paths_for(svc, shared_roles)}
        )
        changed = _changed_files(commit, paths, repo_root, ref)
        if changed is None:
            for svc in services:
                stale[svc] = "commit unknown to this checkout"
            continue
        if not changed:
            continue
        for svc in services:
            svc_paths = role_paths_for(svc, shared_roles)
            hits = [p for p in changed if any(p.startswith(rp) for rp in svc_paths)]
            if hits:
                more = f" (+{len(hits) - 3} more)" if len(hits) > 3 else ""
                stale[svc] = f"changed since applied: {', '.join(hits[:3])}{more}"
    return stale


def _consumes_manifests(role_dir):
    """Whether `role_dir`'s tasks include `k8s/manifests`, the contract that ends in a stamp.

    Not every `containers_list` k8s entry does: `n8n-images` only calls `k8s/image-builder` to
    build n8n's images into the registry and applies no manifests of its own, so it can never
    be stamped and would otherwise read as permanently missing -- the exact "monitor nobody
    trusts" failure `manifest-prune-check.sh.j2`'s header warns against. Same one-level grep the
    repo CLAUDE.md names for this question (`grep -rl k8s/manifests ansible/roles/k8s/*/tasks/`).
    """
    tasks_dir = role_dir / "tasks"
    if not tasks_dir.is_dir():
        return False
    return any("k8s/manifests" in p.read_text() for p in tasks_dir.glob("*.yml"))


def missing_services(records, host_vars=None, k8s_roles_dir=None):
    """k8s-platform service tags, expected to be release-stamped, with no record at all.

    Deployed before the release stamp shipped, or never deployed -- either way this must read
    UNKNOWN rather than being silently excluded from a fleet audit, per issue #947's design.
    Scoped to roles that actually consume `k8s/manifests` (see `_consumes_manifests`); a role
    that never applies manifests never gets a record to be missing.
    """
    deploy_tags = _deploy_tags()
    host_vars = host_vars or deploy_tags.HOST_VARS
    k8s_roles_dir = k8s_roles_dir or (REPO_ROOT / "ansible/roles/k8s")
    known = {
        tag
        for _host, platform, tag in deploy_tags.service_records(host_vars)
        if platform == "k8s" and _consumes_manifests(k8s_roles_dir / tag)
    }
    present = {r.get("service") for r in records if "error" not in r}
    return sorted(known - present)


def format_records(records, merged, service=None, stale=None):
    """Render the release table. Pure: returns (text, exit_code)."""
    if not records:
        return (
            "no release records found in {}\n"
            "Nothing has been deployed since the release stamp shipped -- deploy any k8s "
            "service to write the first one.".format(RELEASE_DIR),
            2,
        )
    if service:
        records = [r for r in records if r.get("service") == service]
        if not records:
            return f"no release record for {service!r}", 2
        return json.dumps(records[0], indent=2), 0

    stale = stale or {}
    lines = [
        f"{'SERVICE':<24} {'COMMIT':<10} {'APPLIED (UTC)':<21} {'FILES':>5}  FLAGS"
    ]
    unclean = 0
    for rec in records:
        if "error" in rec:
            lines.append(
                f"{rec['service']:<24} {'-':<10} {'-':<21} {'-':>5}  UNREADABLE"
            )
            unclean += 1
            continue
        flags = []
        if rec.get("tree_dirty"):
            flags.append("dirty")
        if rec.get("commit") not in merged:
            flags.append("unmerged")
        if rec.get("service") in stale:
            flags.append("stale")
        if flags:
            unclean += 1
        lines.append(
            "{:<24} {:<10} {:<21} {:>5}  {}".format(
                rec.get("service", "?"),
                rec.get("commit_short", "?"),
                rec.get("applied_at", "?"),
                len(rec.get("manifests", {})),
                ",".join(flags) or "-",
            )
        )
    lines.append("")
    lines.append(
        f"{len(records)} service(s); {unclean} carrying a flag. dirty = no commit reproduces "
        "those bytes; unmerged = not an ancestor of origin/master; stale = origin/master has "
        "moved past this record under the service's own or a shared role (`probe.py releases "
        "--stale-only` for the reasons)."
    )
    return "\n".join(lines), (1 if unclean else 0)


def format_stale_only(stale, missing):
    """Render the cron-facing view: one line per stale or record-less service. Pure."""
    lines = [f"{svc}: {reason}" for svc, reason in sorted(stale.items())]
    lines += [f"{svc}: no release record" for svc in missing]
    if not lines:
        return "0 service(s) stale; every known k8s service has a current record.", 0
    return "\n".join(lines), 1


def run_releases(ns):
    """Print the release records (or, with `--json`, raw JSON) and return the exit code.

    Args:
        ns: The parsed argparse namespace for the `releases` subcommand.
    """
    records = load_records(previous=getattr(ns, "previous", False))
    if getattr(ns, "json", False):
        print(json.dumps(records, indent=2))
        return 0
    if getattr(ns, "stale_only", False):
        stale = compute_stale(records)
        missing = missing_services(records)
        text, code = format_stale_only(stale, missing)
        print(text)
        return code
    merged = merged_commits(r.get("commit") for r in records)
    service = getattr(ns, "service", None)
    # Skip the git subprocess work for a single-service lookup or a previous-record read --
    # neither renders the flags table `stale` feeds.
    stale = (
        compute_stale(records)
        if not service and not getattr(ns, "previous", False)
        else {}
    )
    text, code = format_records(records, merged, service=service, stale=stale)
    print(text)
    return code
