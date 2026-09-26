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
(`manifest_affecting_shared_roles()` -- `manifests` and the other entry-less roles that supply
bytes to what is applied), or under an inventory key or shared macro the service's render
reads (`_deploy_plane_stale`, which asks `narrow_broad` the same per-path question the
deployer's tick asks -- #1993). `stale` is what makes a deferred k8s change visible: the
gitops deployer ff-merges a non-auto-deployable k8s role change and pages Discord once, and
every other monitored marker then reads clean while the cluster still runs the old manifests
(issue #947). All three flags are normal mid-slice and alarming a week later, which is why they
are reported rather than judged. A path hit is cleared when a render record proves the applied
bytes are what origin/master renders (`releases_render`, #2586).

Exit codes: 0 when every record is clean, 1 when any service is dirty, unmerged or stale, 2 when
no records exist at all (nothing has been deployed since the stamp shipped). `--stale-only`
answers the narrower question a cron needs: 0 when nothing is stale and every known k8s service
has a record, 1 otherwise.
"""

import json
import re
import subprocess
import time
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

from lib.git import git as _lib_git  # noqa: E402
from lib.repo_paths import REPO as REPO_ROOT  # noqa: E402

# The renderers live in releases_format.py (this module hit the 600-line cap); re-exported so
# `run_releases`, probe.py and the tests keep one name for each.
from diagnostics.probe_lib.releases_format import (  # noqa: E402
    format_records,
    format_stale_kuma,
    format_stale_only,
    write_counted_names,
)

# The one rule that has to read a diff rather than a path lives in its own module, for the
# reason the renderers do: this one is at the 600-line cap.
from diagnostics.probe_lib.releases_diff import drop_check_mode_only  # noqa: E402

# Which services a shared role's bytes reach, and so which paths decide one service's staleness.
# Its own module for the reason above; that module's docstring says what it reuses and why.
from diagnostics.probe_lib.releases_consumers import (  # noqa: E402
    consumers_for,
    role_paths_for,
)

# A render record that proves the applied bytes current clears a path hit (#2586).
from diagnostics.probe_lib.releases_render import apply_renders  # noqa: E402


def _git(*args, cwd, **kwargs):
    """Run git against `cwd` and nothing else, through the shared runner.

    `GIT_DIR`/`GIT_WORK_TREE`/`GIT_INDEX_FILE` in the environment override `cwd`, so a caller
    running this reader from inside another git operation -- prek's own `pytest` hook runs
    under `git commit`, with exactly these set to that commit's in-progress index -- would
    otherwise point every git call in this module at the WRONG repository, and a real commit in
    `repo_root` reads as "commit unknown to this checkout". `lib.git.git` strips every `GIT_*`
    variable, so this module keeps no copy of that fix to drift.
    """
    return _lib_git(*args, cwd=cwd, **kwargs)


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
            rc = _git(
                "merge-base",
                "--is-ancestor",
                commit,
                "origin/master",
                cwd=repo_root,
                check=False,
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


# The role that renders every other role's templates into the applied bytes. It ships no
# templates or files of its own, so the predicate below has to name it.
MANIFEST_RENDERER = "manifests"


def _supplies_manifest_bytes(role_dir):
    """Whether `role_dir` contributes bytes to some service's APPLIED manifests.

    A shared role does that in exactly two ways: it renders templates that are applied
    alongside the consumer's own (`volume-claim/templates/pvc.yaml.j2`,
    `image-builder/templates/build-job.yaml.j2`), or it ships `files/` a consumer's manifest
    embeds with `lookup('file')` (`arr-notification`, `game-stats-lib`). A role with only
    `tasks/` and `defaults/` changes how a deploy RUNS, never what it applies.

    That distinction is the whole point (#1636). A deploy-time role's change is live for the
    next deploy the moment the deployer fast-forwards the primary checkout -- `deploy.sh`
    renders from the tree it is invoked in -- so it invalidates no release stamp and no drift
    exists. Sweeping those roles in marked all 53 services stale for `volume-snapshot`'s
    snapshot-space cap (b7b9bded6), which rendered no manifest at all.

    The same reasoning has a file-granularity twin one level down -- a `tasks/` file inside a
    role that IS in the census supplies no bytes either. `_is_real_change` applies it (#1672).

    `image-builder` stays in, deliberately: its `build-job.yaml.j2` decides the bytes of an
    image nine services then run, which a stamp cannot otherwise see. It is still wider than it
    needs to be -- those nine are named in their own tasks, while this predicate puts the role
    in all 53 services' paths.
    """
    if role_dir.name == MANIFEST_RENDERER:
        return True
    return any((role_dir / sub).is_dir() for sub in ("templates", "files"))


def manifest_affecting_shared_roles(k8s_roles_dir=None, host_vars=None):
    """`shared_k8s_roles()` narrowed to the roles that can make a stamp stale.

    Kept separate from `shared_k8s_roles()` rather than filtering in place: that census answers
    "which derived names are not deploy tags", the question `split_shared_roles` exists for and
    `test_denylist_parsers_agree.py` polices against `deploy_k8s.py`'s own list. Narrowing it
    would change that comparison's meaning as a side effect of fixing this one.
    """
    k8s_roles_dir = k8s_roles_dir or (REPO_ROOT / "ansible/roles/k8s")
    return frozenset(
        r
        for r in shared_k8s_roles(k8s_roles_dir, host_vars)
        if _supplies_manifest_bytes(k8s_roles_dir / r)
    )


# Subdirectories of a shared role that decide how a deploy RUNS rather than what it applies.
# `defaults/` is deliberately absent: `volume-claim/defaults/main.yml` holds `volume_claim_size`
# and `volume_claim_storage_class`, both read by that role's `pvc.yaml.j2`, so a change there
# does move the applied bytes.
_DEPLOY_TIME_SUBDIRS = frozenset({"tasks", "handlers", "meta"})

K8S_ROLES_PREFIX = ("ansible", "roles", "k8s")


def _deploy_time_shared_roles(shared_roles):
    """The shared roles whose `tasks/` cannot move a service's applied manifest bytes.

    `manifests` is excluded because it ships no templates of its own -- its `tasks/` IS the
    render, prune and apply logic that produces every service's bytes, so a change there is
    exactly the false-GREEN issue #947 exists to catch. Every other byte-supplying shared role
    (`volume-claim`, `image-builder`, `arr-notification`, `game-stats-lib`) is in the census for
    its `templates/` or `files/`, and its `tasks/` is deploy-time behaviour.
    """
    return frozenset(shared_roles) - {MANIFEST_RENDERER}


def _is_real_change(path, deploy_time_roles=frozenset()):
    """False for a path that never reaches a deployed manifest.

    Three classes. Docs, because no playbook applies prose. A role's own `tests/`, which holds
    pytest guards over its `files/*.py` and never something `k8s/manifests` stages
    (`ansible/tests/repo/test_no_role_ships_a_test_file.py` enforces that tree-wide). And a
    SHARED role's `tasks/`, which is #1636's role-granularity narrowing applied one level down.

    That third class is the one this repo paid for twice. #1636 dropped the five shared roles
    holding only `tasks/` and `defaults/` from the census, because a deploy-time change is live
    the moment the deployer fast-forwards the primary checkout and invalidates no release stamp.
    The same is true of a `tasks/` file inside a shared role that DOES supply bytes -- but that
    role sits in every service's `role_paths`, so `volume-claim`'s staging-directory move
    (0b86a7d7) marked all 53 services stale and parked `Release Staleness Drift` DOWN with no
    deploy tag able to clear it (#1672). The narrowing is scoped to shared roles: a SERVICE's
    own `tasks/main.yml` names its `manifests_files`, so a change there does move its bytes and
    must still count.
    """
    if path.endswith(".md"):
        return False
    parts = path.split("/")
    if "tests" in parts[:-1]:
        return False
    if (
        len(parts) > 5
        and tuple(parts[:3]) == K8S_ROLES_PREFIX
        and parts[3] in deploy_time_roles
        and parts[4] in _DEPLOY_TIME_SUBDIRS
    ):
        return False
    return True


def _changed_files(commit, paths, repo_root, ref, deploy_time_roles=frozenset()):
    """Real (byte-moving) files under `paths` changed between `commit` and `ref`.

    None means the range could not be resolved -- `commit` is not a rev this checkout knows
    (a pruned worktree branch, a shallow clone) -- which the caller must treat as stale rather
    than silently skip: a commit nobody can find is not evidence the manifests are current.

    `_is_real_change` settles every path on its own, bar one: a SERVICE role's own `tasks/`
    file, where the path says nothing about whether the diff reached a manifest.
    `releases_diff` reads that diff, and only when such a path survived the filter (#2416).
    """
    try:
        result = _git(
            "log",
            "--name-only",
            "--format=",
            f"{commit}..{ref}",
            "--",
            *paths,
            cwd=repo_root,
            timeout=15,
            check=True,
        )
    except OSError, subprocess.SubprocessError:
        return None
    if result.returncode != 0:
        return None
    changed = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    real = sorted(p for p in changed if _is_real_change(p, deploy_time_roles))
    return drop_check_mode_only(
        real, commit, repo_root, ref, K8S_ROLES_PREFIX, MANIFEST_RENDERER
    )


# The derivation line `narrow_broad` emits per key, macro or containers_list entry:
# `narrow: <subject> -> <tag,tag> via <path>`. The subject is what names WHY a service is
# stale ("group_vars/all.yml (lan_subnet)" rather than the file alone), and this is the one
# place it is available -- the rules return tags, not the key that reached them.
_NARROW_LINE = re.compile(r"^narrow: (\S+) -> (\S*) via (\S+)$")


def _narrow_broad():
    """Import `scripts/deploy_tools/narrow_broad` lazily.

    For the reason `_deploy_tags` gives: it parses host_vars YAML and walks the role tree
    on import, which only `releases` needs.
    """
    from deploy_tools import narrow_broad

    return narrow_broad


def _deploy_plane_stale(commit, services, changed, context):
    """{service: [hit, ...]} for `changed`, the deploy-plane paths moved since `commit`.

    The gap this closes (#1993). `role_paths_for` covers a service's own role and the shared
    roles, so a change under `ansible/inventory/` or `ansible/templates/` moved nothing this
    reader read: a denied role whose render reads a changed key sat behind a clean monitor
    until something unrelated redeployed it. The deployer's tick already derives which tags
    such a change reaches (`narrow_broad`, the DECIDED at `deploy_narrow.denylisted_in`), so
    this asks the same question per path, from the record's commit to `ref`.

    A path no rule can attribute -- a key the play itself reads, `hosts.ini` -- marks EVERY
    service sharing `commit` stale, with the refusal as the
    reason. That is the tick's own answer to the same doubt: it runs the whole play, which
    re-stamps every service, so the set this marks is exactly the set that run refreshes. It
    is not the #1672 shape -- a deploy-time change flagging the fleet with no tag able to
    clear it -- because the full run the tick takes for that range IS the clear, and the
    only records left behind it are ones a hand deploy from an older tree wrote, which is the
    incident this module's docstring opens with. Measured over the 600 commits to
    2026-09-18: two refusals (a play-read key, a removed entry), at most 1.7s per record commit.
    The tick honoured that contract for a range carrying ONLY the deploy plane until
    2026-09-18: a mixed range planned the setup half alone, and a removed Pi entry beside a
    `roles/setup/` edit left the fleet marked here with no run to clear it (#2046).

    `context` is built by the caller, once per `compute_stale`, because it reads host_vars
    at its ref and walks the role tree. Only called when the range changed a census path, so
    a repo with no host_vars at all still reads clean for a role-only range.
    """
    nb = _narrow_broad()
    hits = {svc: [] for svc in services}
    for path in changed:
        subjects = {}

        def explain(message, _subjects=subjects):
            m = _NARROW_LINE.match(message)
            if m:
                for tag in m.group(2).split(","):
                    _subjects.setdefault(tag, []).append(m.group(1))

        ctx = context._replace(explain=explain)
        try:
            tags = nb.broad_path_tags(path, commit, ctx)
        except nb.CannotNarrow as exc:
            for svc in services:
                hits[svc].append(f"{path} [every service: {exc}]")
            continue
        for svc in services:
            if svc in tags:
                why = ", ".join(subjects.get(svc, [])) or "reached"
                hits[svc].append(f"{path} ({why})")
    return {svc: paths for svc, paths in hits.items() if paths}


def _drift_started(commit, hits, repo_root, ref, _memo=None):
    """Committer time (epoch seconds) of the OLDEST commit in `commit..ref` touching `hits`.

    Oldest, because the grace window bounds how long a service has run old manifests, and
    that clock starts at the first un-applied change. The newest would let every fresh commit
    on a busy path -- `roles/k8s/manifests/` is an offending path for the whole fleet --
    reset the clock on drift that is already days old.

    `hits` are the reason strings `compute_stale` built -- a path, optionally followed by a
    deploy-plane annotation -- so the path is the first token. Committer time rather than
    author time: a squash merge keeps the author's clock, and the moment a grace period
    counts from is when the change reached master. None when git cannot answer, which the
    caller treats as old: a range it cannot date is not evidence the change is fresh.

    `_memo` is keyed on the range and paths: a refused narrowing hands every service on a
    commit the same hit, and without it a fleet-wide DOWN would pay one `git log` per
    service, against the one-call-per-commit budget `compute_stale`'s docstring promises.
    """
    paths = tuple(sorted({hit.split(" ", 1)[0] for hit in hits}))
    key = (commit, ref, paths)
    if _memo is not None and key in _memo:
        return _memo[key]
    try:
        result = _git(
            "log",
            "--format=%ct",
            f"{commit}..{ref}",
            "--",
            *paths,
            cwd=repo_root,
            timeout=15,
            check=True,
        )
        # `git log` prints newest first, and `-n` is applied before `--reverse`, so the
        # oldest is the last line rather than anything a `-1` could select.
        started = int(result.stdout.split()[-1])
    except OSError, subprocess.SubprocessError, ValueError, IndexError:
        started = None
    if _memo is not None:
        _memo[key] = started
    return started


def compute_stale(
    records,
    repo_root=REPO_ROOT,
    ref="origin/master",
    shared_roles=None,
    consumers=None,
    declared=None,
    callers=None,
    grace_seconds=0,
    now=None,
    pending=None,
):
    """{service: reason} for every record whose own, shared or deploy-plane paths changed since `ref`.

    `consumers` decides WHICH services a shared role's change can make stale
    (`releases_consumers.consumers_for`, read from `repo_root`'s caller graph when omitted).

    `declared` and `callers` are `narrow_broad.context_for`'s two derived fields, read from
    `repo_root` at `ref` when omitted. They are parameters so a test can drive a throwaway
    repo that declares no host_vars; production never passes them.

    `grace_seconds` is the window a merge gets before its drift counts. The monitor pushed
    DOWN on the first */30 run after ANY merge, which caught code-server at 13:00 on
    2026-09-21 while its own landing had been building the image since 12:50. A service
    whose OLDEST offending commit reached `ref` less than `grace_seconds` ago is left out of
    the result and written to `pending` ({service: seconds since that commit}) when the
    caller passes a dict (`_drift_started` says why oldest). A range git cannot date stays
    stale. `now` is epoch seconds, for the tests. The default of 0 keeps every caller that
    never asked for a grace on the old contract.

    One `git log` per distinct commit, not per service -- a full deploy stamps ~54 records
    sharing one commit, and grouping first keeps this from being 54 subprocess calls for what a
    single one already answers for the union of every service's role_paths.
    """
    shared_roles = (
        shared_roles if shared_roles is not None else manifest_affecting_shared_roles()
    )
    deploy_time_roles = _deploy_time_shared_roles(shared_roles)
    if consumers is None:
        consumers = consumers_for(shared_roles, repo_root, renderer=MANIFEST_RENDERER)
    by_commit = {}
    for rec in records:
        if "error" in rec:
            continue
        commit, service = rec.get("commit"), rec.get("service")
        if not commit or not service:
            continue
        by_commit.setdefault(commit, []).append(service)

    stale = {}
    dated = {}
    context = None
    for commit, services in by_commit.items():
        paths = sorted(
            {
                p
                for svc in services
                for p in role_paths_for(svc, shared_roles, consumers)
            }
        )
        changed = _changed_files(commit, paths, repo_root, ref, deploy_time_roles)
        if changed is None:
            for svc in services:
                stale[svc] = "commit unknown to this checkout"
            continue
        # The context is built on the first commit whose range changed a census path, and
        # never for a range that changed none: reading it costs a host_vars parse at `ref`
        # and a walk of the role tree, and a repo with no host_vars (the role-only test
        # repos) would fail the read for a question nobody asked.
        plane_changed = _changed_files(
            commit, _narrow_broad().CENSUS_PREFIXES, repo_root, ref
        )
        plane = {}
        if plane_changed:
            if context is None:
                context = _narrow_broad().context_for(
                    ref, repo_root, declared=declared, callers=callers
                )
            plane = _deploy_plane_stale(commit, services, plane_changed, context)
        for svc in services:
            svc_paths = role_paths_for(svc, shared_roles, consumers)
            hits = [p for p in changed if any(p.startswith(rp) for rp in svc_paths)]
            hits += plane.get(svc, [])
            if not hits:
                continue
            if grace_seconds > 0:
                started = _drift_started(commit, hits, repo_root, ref, _memo=dated)
                if started is not None:
                    age = (now if now is not None else time.time()) - started
                    if age < grace_seconds:
                        if pending is not None:
                            pending[svc] = int(age)
                        continue
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
        grace_seconds = int(getattr(ns, "grace_minutes", 0) or 0) * 60
        pending = {}
        stale = compute_stale(records, grace_seconds=grace_seconds, pending=pending)
        apply_renders(stale, records, pending=pending)
        missing = missing_services(records)
        write_counted_names(getattr(ns, "names_out", None), stale, missing)
        render = format_stale_kuma if getattr(ns, "kuma", False) else format_stale_only
        text, code = render(
            stale, missing, pending=pending, grace_seconds=grace_seconds
        )
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
    apply_renders(stale, records)
    text, code = format_records(
        records, merged, service=service, stale=stale, release_dir=RELEASE_DIR
    )
    print(text)
    return code
