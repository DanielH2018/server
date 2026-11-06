"""The render digest, which is `probe.py releases`' primary answer on staleness.

WHY THIS EXISTS. `releases.compute_stale` decides staleness from the paths a merge
touched, and a path is a proxy: five narrowings exist because a path moved while the rendered
bytes did not. A render-mode dry run (`-e k8s_dry_run=true -e manifests_render_record=true`)
writes `k8s-renders.d/<service>.json`, whose `manifests_digest` comes from the same
`release_digest.yml` as the release record's. The two digests therefore identify the same
bytes, and comparing them answers all five narrowings at once.

THE LADDER. `digest_verdict` returns one of three answers for a service, and the first two
outrank the path check:

  * `CURRENT` -- the digests agree, so the bytes the apply wrote are the bytes the ref renders.
    A path hit under the service's own role, `manifests` or the deploy plane is a false
    positive and is dropped. A hit under any other shared role is kept, because that role acts
    outside the digest (`undigested_hits`, #3090).
  * `DRIFTED` -- the record is trustworthy and the digests disagree, so the applied bytes are
    not what the ref renders. The service is stale for that reason, WHETHER OR NOT a path
    moved. This is what #3046 inverted: until then a digest could only clear a path hit, so a
    service the path narrowing read clean could drift unseen.
  * `UNKNOWN` -- no record, or one that proves nothing (below). The path verdict stands
    unchanged, which is the whole of the pre-#3046 behaviour.

WHAT MAKES A RECORD PROVE NOTHING. A digest is evidence about the ref only when the render
record's `commit` IS the ref, its tree was clean, and its `host` is the release record's,
because a digest from another commit, a dirty tree or another host's vars names different
bytes. `manifests_digest`
also excludes secret manifests by design, and uptime-kuma's `static-monitors.yaml` is one: a
monitor added there leaves the digest identical while the path check correctly reads it stale.
The `secret_digest` HMAC covers that, so a service with secret manifests needs that field on
BOTH records; one written before it, or one whose host key was unreadable ('' by design), falls
back to the path verdict and the fleet converges one redeploy at a time.

A RENDER BEHIND THE REF (#4881). The producer renders hourly and master merges more often, so a
record whose commit is the ref is the exception. A render of an ANCESTOR R of the ref that
meets every other rule above, and whose digest matches, still proves the applied bytes are what
R renders. Every path hit R already holds is then a false positive, so `apply_verdicts` drops
it, the same way `CURRENT` does. A hit touched again in R..ref keeps the path verdict, and so
does an `undigested_hits` hit wherever it falls.

The grace window counts from the oldest commit R does not answer for, hit by hit, and the
service's date is the oldest over its kept hits. A digested hit kept because R..ref touched it
dates from its oldest commit in R..ref. An undigested hit dates from its oldest commit in the
release record's `commit..ref`, since R answers for none of it. So a path touched only after R
dates from its own recent commit and gets grace, while one touched before R keeps its old date
and reads stale. A dropped hit dates nothing. That is what keeps grace meaning "a merge younger
than N minutes" once R clears the older hits. The window's length is unchanged.

A MISMATCH at R proves nothing about the ref, since R..ref may hold the fix, so it reads
`UNKNOWN`, never `DRIFTED`. So does a render whose `commit` is not a full 40-character
lowercase SHA, since that value reaches git.

AN UNEXPLAINED MISMATCH (#5903). The path verdict still stands for a mismatch at R, but its
reason can name a hit that moved no bytes. #4840's homelab-mcp read `changed since applied:
.../release_stamp.yml`, a comment-only edit, while the drift came from a build-context file no
path rule read. So `unexplained_behind` asks whether ANY hit could have moved the bytes, and
when none could, `apply_verdicts` replaces the path reason with `_UNEXPLAINED_REASON`. The
service stays stale and keeps its place; only the reason changes. A hit could have moved the
bytes unless its change from the release record's commit C is inert both at R and at the ref:
Ansible-read YAML whose content lines are equal at both ends, or a template whose edit is
Jinja comments only. R is the end that matters, since R's render is the one that mismatched;
the ref is checked too so the reason never hides a hit the path verdict would name. An edit
at R that the ref reverts is therefore still a cause. An `undigested_hits` hit always counts, since it acts outside the digest.
The claim also needs C to be an ancestor of R, or R itself: a render older than the apply
mismatches because of R..C, which no hit names. This reads `manifests/tasks` comments, which
`releases_diff` declines to narrow, and that is safe here because the answer changes a label,
never whether the service is stale.

TWO THINGS A DRIFTED VERDICT DOES NOT OVERRIDE. A service inside `compute_stale`'s grace window
keeps waiting: the window exists because a landing's own deploy is still in flight, and a digest
cannot tell that apart from drift. And "commit unknown to this checkout" is a doubt about
PROVENANCE -- nobody can say where the applied bytes came from -- which no digest speaks to, so
it stays reported as it was.

THE EVALUATION ORDER IS NOT THE PRECEDENCE ORDER. `run_releases` runs `compute_stale` first and
then applies the verdicts over its result. The digest still decides -- it replaces or creates
the reason for every service it can answer for -- but computing it first would mean skipping
the path work for a `CURRENT` service, and that work is where the unknown-commit and grace
answers come from.
"""

import json
import re
import subprocess
import time
from pathlib import Path

# The same `scripts/` bootstrap releases.py carries, for its reason.
import sys as _sys

_sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from lib.git import git
from lib.repo_paths import GITOPS_DEPLOY_FILES, REPO as REPO_ROOT

# Mirrors manifests_render_record_dir in roles/k8s/manifests/defaults/main.yml;
# scripts/diagnostics/tests/test_probe_releases_render.py asserts the two agree.
RENDER_DIR = Path("/var/lib/homelab/k8s-renders.d")

# The prefix `compute_stale` gives a reason built from path or deploy-plane hits.
PATH_HIT_PREFIX = "changed since applied: "

# The shared roles whose whole effect is bytes `manifests_digest` covers, so a matching digest
# proves their change applied. Mirrors `gitops_markers.RENDER_PROOF_SHARED_ROLES`, the
# deployer's copy since D6.12 deleted `deploy_k8s_owed.py`; a test pins the two equal.
DIGEST_PROVABLE_ROLES = frozenset({"manifests"})

_K8S_ROLES = "ansible/roles/k8s/"

# A commit a record names reaches `git merge-base` and `git log`, so only a full lowercase
# object name is passed on: not a ref, an option, an abbreviation or an uppercase spelling.
_FULL_SHA = re.compile(r"[0-9a-f]{40}")

# The three answers `digest_verdict` returns.
CURRENT = "current"
DRIFTED = "drifted"
UNKNOWN = "unknown"

# The prefix a DRIFTED reason carries, so `releases_format._kuma_reason` and a reader can tell
# a digest verdict from a path one at a glance.
DIGEST_REASON_PREFIX = "render digest differs: "

_MANIFESTS_REASON = (
    DIGEST_REASON_PREFIX + "the applied manifests are not what origin/master renders"
)
_SECRETS_REASON = (
    DIGEST_REASON_PREFIX
    + "the applied secret manifests are not what origin/master renders"
)
_UNEXPLAINED_REASON = (
    DIGEST_REASON_PREFIX
    + "the applied manifests are not what an earlier origin/master renders,"
    + " and no change since the apply explains it"
)

# The Ansible-read directories, whose YAML content lines are all a run sees. A `#` line in a
# `templates/` or `files/` YAML is usually copied into a manifest, so those never qualify.
_ANSIBLE_READ_DIRS = frozenset({"tasks", "defaults", "vars", "handlers", "meta"})


def load_renders(render_dir=RENDER_DIR):
    """{service: render record} for every record in `render_dir` that parses.

    An unparseable record is dropped rather than reported: its absence only keeps the path
    verdict, which is the safe reading.
    """
    renders = {}
    if not render_dir.is_dir():
        return renders
    for path in sorted(render_dir.glob("*.json")):
        try:
            rec = json.loads(path.read_text())
        except OSError, ValueError:
            continue
        if isinstance(rec, dict) and rec.get("service"):
            renders[rec["service"]] = rec
    return renders


def resolve_ref(ref, repo_root=REPO_ROOT):
    """The full SHA `ref` names in `repo_root`, or None when git cannot say."""
    try:
        result = git(
            "rev-parse", "--verify", ref, cwd=repo_root, check=False, timeout=10
        )
    except OSError, subprocess.SubprocessError:
        return None
    sha = result.stdout.strip()
    return sha if result.returncode == 0 and len(sha) == 40 else None


def _comparable(release, render, ref_sha):
    """Whether the two records' digests identify bytes that can be compared at all.

    False for every refusal in the module docstring's "prove nothing" list. A False here is
    `UNKNOWN`, never `DRIFTED`: the records disagree about something other than the bytes, so
    a mismatch would be evidence of nothing.

    The secret half asks only whether the FIELD is present on both records, not whether it
    matches -- a present-and-different `secret_digest` is real drift, which `digest_verdict`
    reports. A record predating the field, and a `''` written when the host key was unreadable,
    are the two absences.
    """
    if not release or not render or not ref_sha:
        return False
    if render.get("commit") != ref_sha or render.get("tree_dirty") is not False:
        return False
    # An older release record carries no host. That is "unknown", never a match.
    if not release.get("host") or release.get("host") != render.get("host"):
        return False
    if not release.get("manifests_digest") or not render.get("manifests_digest"):
        return False
    names, render_names = (
        release.get("secret_manifests"),
        render.get("secret_manifests"),
    )
    if names is None or render_names is None:
        return False
    if names or render_names:
        return bool(release.get("secret_digest")) and bool(render.get("secret_digest"))
    return True


def digest_verdict(release, render, ref_sha):
    """`(verdict, reason)` for one service: CURRENT, DRIFTED or UNKNOWN.

    The reason is the empty string for every answer but DRIFTED, which is the only one that
    becomes a line an operator reads.
    """
    if not _comparable(release, render, ref_sha):
        return UNKNOWN, ""
    if release["manifests_digest"] != render["manifests_digest"]:
        return DRIFTED, _MANIFESTS_REASON
    names = release.get("secret_manifests")
    if names != render.get("secret_manifests"):
        return DRIFTED, _SECRETS_REASON
    if names and release.get("secret_digest") != render.get("secret_digest"):
        return DRIFTED, _SECRETS_REASON
    return CURRENT, ""


def render_proves_current(release, render, ref_sha):
    """Whether `render` shows `release`'s applied bytes are what `ref_sha` renders."""
    return digest_verdict(release, render, ref_sha)[0] == CURRENT


def verdicts_for(records, renders, ref_sha):
    """{service: (verdict, reason)} for every parseable release record in `records`."""
    return {
        rec["service"]: digest_verdict(rec, renders.get(rec["service"]), ref_sha)
        for rec in records
        if "error" not in rec and rec.get("service")
    }


def _touch_times(rev_range, paths=(), repo_root=REPO_ROOT):
    """{path: oldest committer time} for every path `git log rev_range` touches, or None.

    `paths`, when given, limits the log to those pathspecs. None means git could not answer,
    which a caller treats as undated: a range git cannot read is not evidence a change is fresh.
    """
    try:
        result = git(
            "log",
            "--name-only",
            "--format=%x00%ct",
            rev_range,
            *(("--", *paths) if paths else ()),
            cwd=repo_root,
            check=False,
            timeout=15,
        )
    except OSError, subprocess.SubprocessError:
        return None
    if result.returncode != 0:
        return None
    started = {}
    # Newest first, so the last time recorded for a path is its oldest.
    for entry in result.stdout.split("\0")[1:]:
        lines = [line.strip() for line in entry.splitlines() if line.strip()]
        if not lines:
            continue
        for path in lines[1:]:
            started[path] = int(lines[0])
    return started


def _is_ancestor(commit, ref_sha, repo_root=REPO_ROOT):
    """Whether `commit` is `ref_sha` or one of its ancestors. False when git cannot say."""
    try:
        result = git(
            "merge-base",
            "--is-ancestor",
            commit,
            ref_sha,
            cwd=repo_root,
            check=False,
            timeout=10,
        )
    except OSError, subprocess.SubprocessError:
        return False
    return result.returncode == 0


def changed_since(commit, ref_sha, repo_root=REPO_ROOT):
    """{path: oldest committer time} for every path touched in `commit..ref_sha`.

    None when `commit` is not an ancestor of `ref_sha`, or git cannot say: a render of a commit
    the ref does not hold, or one this checkout does not know, proves nothing about the ref.

    The log is raw, so a path whose only edit after `commit` is comment-only still counts. That
    keeps a hit the path rules would drop, which is the safe direction.
    """
    if not _is_ancestor(commit, ref_sha, repo_root):
        return None
    return _touch_times(f"{commit}..{ref_sha}", repo_root=repo_root)


def renders_behind(records, renders, ref_sha, verdicts, repo_root=REPO_ROOT, hits=None):
    """{service: {path: date}} for each service a render behind `ref_sha` clears.

    That is a service `verdicts` leaves `UNKNOWN` whose render's commit R is a full SHA, an
    ancestor of `ref_sha`, and `CURRENT` at R by the same rules `digest_verdict` applies at the
    ref. `apply_verdicts` reads one as `CURRENT` for every path hit R already holds. One git
    range per distinct R, since the producer renders every service at one commit.

    The dates are the epoch seconds each kept hit counts from. A path R..ref touched dates from
    its oldest commit there, because R answers for everything before it. An `undigested_hits`
    path in `hits` dates from its oldest commit in the release record's `commit..ref_sha`,
    because R answers for none of it. A path with no date is absent, and the caller reads it
    as old.
    """
    ranges, behind = {}, {}
    for rec in records:
        service = rec.get("service")
        render = renders.get(service)
        if verdicts.get(service, (None,))[0] != UNKNOWN or not render:
            continue
        commit = render.get("commit")
        # The commit reaches git, so only a full lowercase object name is passed on.
        if not _is_full_sha(commit) or commit == ref_sha:
            continue
        if digest_verdict(rec, render, commit)[0] != CURRENT:
            continue
        if commit not in ranges:
            ranges[commit] = changed_since(commit, ref_sha, repo_root)
        if ranges[commit] is None:
            continue
        dates = dict(ranges[commit])
        base = rec.get("commit")
        undigested = sorted(
            {
                _hit_path(h)
                for h in undigested_hits(service, (hits or {}).get(service, ()))
            }
        )
        if undigested and _is_full_sha(base):
            dates.update(
                _touch_times(f"{base}..{ref_sha}", undigested, repo_root) or {}
            )
        behind[service] = dates
    return behind


def unexplained_behind(records, renders, ref_sha, verdicts, hits, repo_root=REPO_ROOT):
    """The services whose render behind `ref_sha` mismatches with no hit able to explain it.

    That is a service `verdicts` leaves `UNKNOWN` whose render's commit R is a full SHA, an
    ancestor of `ref_sha`, and `DRIFTED` at R by `digest_verdict`'s rules. Its release record's
    commit C is a full SHA from a clean tree, and C is R or an ancestor of it. Every hit in
    `hits` is inert from C to R and from C to `ref_sha`, by `_inert`, and none is one `undigested_hits` keeps.
    The module docstring says why each condition is there.
    """
    found, inert = set(), {}
    for rec in records:
        service = rec.get("service")
        render, service_hits = renders.get(service), hits.get(service)
        if (
            verdicts.get(service, (None,))[0] != UNKNOWN
            or not render
            or not service_hits
        ):
            continue
        commit, base = render.get("commit"), rec.get("commit")
        if not _is_full_sha(commit) or not _is_full_sha(base) or commit == ref_sha:
            continue
        if rec.get("tree_dirty") is not False or undigested_hits(service, service_hits):
            continue
        if digest_verdict(rec, render, commit)[0] != DRIFTED:
            continue
        if not _is_ancestor(commit, ref_sha, repo_root):
            continue
        if not _is_ancestor(base, commit, repo_root):
            continue
        for path in {_hit_path(h) for h in service_hits}:
            for end in (commit, ref_sha):
                if (path, base, end) not in inert:
                    inert[path, base, end] = _inert(path, base, end, repo_root)
        if all(
            inert[_hit_path(h), base, end]
            for h in service_hits
            for end in (commit, ref_sha)
        ):
            found.add(service)
    return found


def _inert(path, old_ref, new_ref, repo_root=REPO_ROOT):
    """Whether `path`'s change from `old_ref` to `new_ref` cannot move a rendered byte.

    True for Ansible-read YAML whose content lines are equal at both refs, by the deployer's
    own `_content_unchanged`, and for a `.j2` whose edit is Jinja comments only, by
    `narrow_lib.templates.comment_only`. True for any path whose text is identical at both
    refs. False for every other path, and for a side git cannot read.
    """
    from lib.narrow_git import show_at

    before, after = (show_at(ref, path, repo_root) for ref in (old_ref, new_ref))
    if before is None or after is None:
        return False
    if before == after:
        return True
    if path.endswith(".j2"):
        from deploy_tools.narrow_lib.templates import comment_only

        return comment_only(before, after)
    parts = path.split("/")
    ansible_read = path.startswith("ansible/inventory/") or (
        path.startswith(_K8S_ROLES)
        and len(parts) > 5
        and parts[4] in _ANSIBLE_READ_DIRS
    )
    if not ansible_read or not path.endswith((".yml", ".yaml")):
        return False
    if str(GITOPS_DEPLOY_FILES) not in _sys.path:
        _sys.path.insert(0, str(GITOPS_DEPLOY_FILES))
    from deployer_lib.deploy_changes import _content_unchanged

    texts = {old_ref: before, new_ref: after}
    return bool(_content_unchanged([path], old_ref, new_ref, lambda r, _p: texts[r]))


def _is_full_sha(value):
    """Whether `value` is exactly 40 lowercase hex characters."""
    return isinstance(value, str) and _FULL_SHA.fullmatch(value) is not None


def path_reason(hits):
    """The `PATH_HIT_PREFIX` reason for `hits`, naming the first three."""
    more = f" (+{len(hits) - 3} more)" if len(hits) > 3 else ""
    return f"{PATH_HIT_PREFIX}{', '.join(hits[:3])}{more}"


def undigested_hits(service, hits):
    """The `hits` a matching digest cannot clear for `service`.

    That is a hit under a shared k8s role outside `DIGEST_PROVABLE_ROLES`. `image-builder`'s build Job
    is outside the digest, and `arr-notification` writes an app's database over its API. Each
    changes what is live while every caller's digest stays CURRENT, so clearing its hit on a
    digest match would hide a change nothing has applied. A hit under the service's own role,
    or a deploy-plane hit outside `ansible/roles/k8s/`, is one the digest answers for.
    """
    kept = []
    for hit in hits:
        if hit.startswith(_K8S_ROLES):
            role = hit[len(_K8S_ROLES) :].split("/", 1)[0]
            if role != service and role not in DIGEST_PROVABLE_ROLES:
                kept.append(hit)
    return kept


def _hit_path(hit):
    """The path a `compute_stale` hit names, without a deploy-plane annotation after it."""
    return hit.split(" ", 1)[0]


def apply_verdicts(
    stale,
    pending,
    verdicts,
    hits=None,
    behind=None,
    grace_seconds=0,
    now=None,
    unexplained=(),
):
    """Let the digest answer override the path verdict in `stale` and `pending`, in place.

    Returns the sorted names a CURRENT verdict cleared from `stale`.

    `hits` is `compute_stale`'s `hits_out`: every path hit per service, where the reason names
    three. A CURRENT verdict clears a service only when none of its hits is one
    `undigested_hits` keeps. A stale service with such a hit stays stale, its reason narrowed
    to the kept hits, and a pending one stays pending.

    `behind` is `renders_behind`'s answer: {service: {path: date}}. A service in it is CURRENT
    at its render's commit R, so it keeps every `undigested_hits` hit and every hit whose path
    R..ref touched. The drift date is the oldest `date` among the kept hits, and a kept hit
    with no date counts as old. A stale service whose drift is younger than `grace_seconds`
    moves to `pending`, as `compute_stale` would have placed it. `now` is epoch seconds, for
    the tests.

    A CURRENT service is dropped from `pending` too, so it does not read as a merge still
    waiting on its deploy. A DRIFTED one is left alone where the grace window or the
    unknown-commit doubt already answers for it, per the module docstring.

    `unexplained` is `unexplained_behind`'s answer. A stale service in it keeps its place, and
    its path reason becomes `_UNEXPLAINED_REASON`.
    """
    cleared = []
    behind = behind or {}
    for service, (verdict, reason) in sorted(verdicts.items()):
        # Without the hits there is no telling which ones R holds, so nothing is dropped.
        if service in behind and hits is not None:
            verdict = CURRENT
        if verdict == CURRENT:
            service_hits = (hits or {}).get(service, ())
            kept = undigested_hits(service, service_hits)
            dates = behind.get(service)
            if dates is not None:
                kept = [h for h in service_hits if h in kept or _hit_path(h) in dates]
            age = _drift_age(kept, dates, now) if dates is not None else None
            if service in stale and stale[service].startswith(PATH_HIT_PREFIX):
                if not kept:
                    del stale[service]
                    cleared.append(service)
                elif (
                    pending is not None
                    and age is not None
                    and grace_seconds > 0
                    and age < grace_seconds
                ):
                    del stale[service]
                    pending[service] = age
                else:
                    stale[service] = path_reason(kept)
            if pending and service in pending:
                if not kept:
                    del pending[service]
                elif age is not None:
                    pending[service] = age
        elif verdict == DRIFTED:
            if pending and service in pending:
                continue
            if service in stale and not stale[service].startswith(PATH_HIT_PREFIX):
                continue
            stale[service] = reason
        elif service in unexplained and service in stale:
            if stale[service].startswith(PATH_HIT_PREFIX):
                stale[service] = _UNEXPLAINED_REASON
    return cleared


def _drift_age(kept, dates, now):
    """Seconds since the oldest kept hit's date, or None when any kept hit has no date.

    A hit git could not date is not evidence the change is fresh, so it leaves the age unknown
    and the caller keeps the service stale.
    """
    if not kept or any(_hit_path(h) not in dates for h in kept):
        return None
    started = min(dates[_hit_path(h)] for h in kept)
    return int((now if now is not None else time.time()) - started)


def apply_digest_verdicts(
    stale,
    records,
    pending=None,
    repo_root=REPO_ROOT,
    ref="origin/master",
    render_dir=None,
    hits=None,
    grace_seconds=0,
    now=None,
):
    """`apply_verdicts` for the render records in `render_dir` and `ref` resolved in `repo_root`.

    `grace_seconds` and `now` are `compute_stale`'s, for the re-dating `apply_verdicts` does.
    """
    renders = load_renders(render_dir or RENDER_DIR)
    if not renders:
        return []
    ref_sha = resolve_ref(ref, repo_root)
    verdicts = verdicts_for(records, renders, ref_sha)
    behind = (
        renders_behind(records, renders, ref_sha, verdicts, repo_root, hits)
        if ref_sha
        else {}
    )
    unexplained = (
        unexplained_behind(records, renders, ref_sha, verdicts, hits, repo_root)
        if ref_sha and hits
        else set()
    )
    return apply_verdicts(
        stale,
        pending,
        verdicts,
        hits=hits,
        behind=behind,
        grace_seconds=grace_seconds,
        now=now,
        unexplained=unexplained,
    )
