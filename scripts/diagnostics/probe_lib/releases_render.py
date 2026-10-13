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
does an `undigested_hits` hit wherever it falls. When every hit left is one R..ref touched,
the grace window counts from the oldest commit in R..ref touching them, because the hits
before R no longer say when drift started. A MISMATCH at R proves nothing about the ref, since
R..ref may hold the fix, so it reads `UNKNOWN`, never `DRIFTED`.

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
import subprocess
import time
from pathlib import Path

# The same `scripts/` bootstrap releases.py carries, for its reason.
import sys as _sys

_sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from lib.git import git
from lib.repo_paths import REPO as REPO_ROOT

# Mirrors manifests_render_record_dir in roles/k8s/manifests/defaults/main.yml;
# scripts/diagnostics/tests/test_probe_releases_render.py asserts the two agree.
RENDER_DIR = Path("/var/lib/homelab/k8s-renders.d")

# The prefix `compute_stale` gives a reason built from path or deploy-plane hits.
PATH_HIT_PREFIX = "changed since applied: "

# The shared roles whose whole effect is bytes `manifests_digest` covers, so a matching digest
# proves their change applied. Mirrors `deploy_k8s_owed.DIGEST_PROVABLE_ROLES`, whose DECIDED
# marker says what each other shared role does outside the digest; a test pins the two equal.
DIGEST_PROVABLE_ROLES = frozenset({"manifests"})

_K8S_ROLES = "ansible/roles/k8s/"

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


def changed_since(commit, ref_sha, repo_root=REPO_ROOT):
    """{path: oldest committer time} for every path touched in `commit..ref_sha`.

    None when `commit` is not an ancestor of `ref_sha`, or git cannot say: a render of a commit
    the ref does not hold, or one this checkout does not know, proves nothing about the ref.

    The log is raw, so a path whose only edit after `commit` is comment-only still counts. That
    keeps a hit the path rules would drop, which is the safe direction.
    """
    try:
        if (
            git(
                "merge-base",
                "--is-ancestor",
                commit,
                ref_sha,
                cwd=repo_root,
                check=False,
                timeout=10,
            ).returncode
            != 0
        ):
            return None
        result = git(
            "log",
            "--name-only",
            "--format=%x00%ct",
            f"{commit}..{ref_sha}",
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


def renders_behind(records, renders, ref_sha, verdicts, repo_root=REPO_ROOT):
    """{service: `changed_since` its render} for each service a render behind `ref_sha` clears.

    That is a service `verdicts` leaves `UNKNOWN` whose render's commit R is an ancestor of
    `ref_sha` and whose digest is `CURRENT` at R, by the same rules `digest_verdict` applies at
    the ref. `apply_verdicts` reads one as `CURRENT` for every path hit R already holds. One git
    range per distinct R, since the producer renders every service at one commit.
    """
    ranges, behind = {}, {}
    for rec in records:
        service = rec.get("service")
        render = renders.get(service)
        if verdicts.get(service, (None,))[0] != UNKNOWN or not render:
            continue
        commit = render.get("commit")
        if not commit or commit == ref_sha:
            continue
        if digest_verdict(rec, render, commit)[0] != CURRENT:
            continue
        if commit not in ranges:
            ranges[commit] = changed_since(commit, ref_sha, repo_root)
        if ranges[commit] is not None:
            behind[service] = ranges[commit]
    return behind


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
    stale, pending, verdicts, hits=None, behind=None, grace_seconds=0, now=None
):
    """Let the digest answer override the path verdict in `stale` and `pending`, in place.

    Returns the sorted names a CURRENT verdict cleared from `stale`.

    `hits` is `compute_stale`'s `hits_out`: every path hit per service, where the reason names
    three. A CURRENT verdict clears a service only when none of its hits is one
    `undigested_hits` keeps. A stale service with such a hit stays stale, its reason narrowed
    to the kept hits, and a pending one stays pending.

    `behind` is `renders_behind`'s answer. A service in it is CURRENT at its render's commit R,
    so it keeps, as well, every hit whose path R..ref touched. When every hit it keeps is one of
    those, it is re-dated from R: a stale service whose oldest such commit is younger than
    `grace_seconds` moves to `pending`, as `compute_stale` would have placed it. `now` is epoch
    seconds, for the tests.

    A CURRENT service is dropped from `pending` too, so it does not read as a merge still
    waiting on its deploy. A DRIFTED one is left alone where the grace window or the
    unknown-commit doubt already answers for it, per the module docstring.
    """
    cleared = []
    behind = behind or {}
    for service, (verdict, reason) in sorted(verdicts.items()):
        # Without the hits there is no telling which ones R holds, so nothing is dropped.
        if service in behind and hits is not None:
            verdict = CURRENT
        if verdict == CURRENT:
            service_hits = (hits or {}).get(service, ())
            undigested = undigested_hits(service, service_hits)
            after = behind.get(service, {})
            redated = [h for h in service_hits if _hit_path(h) in after]
            kept = [h for h in service_hits if h in undigested or h in redated]
            if service in stale and stale[service].startswith(PATH_HIT_PREFIX):
                if not kept:
                    del stale[service]
                    cleared.append(service)
                elif pending is not None and _within_grace(
                    kept, redated, after, grace_seconds, now
                ):
                    del stale[service]
                    pending[service] = _age(kept, after, now)
                else:
                    stale[service] = path_reason(kept)
            if pending and service in pending and not kept:
                del pending[service]
        elif verdict == DRIFTED:
            if pending and service in pending:
                continue
            if service in stale and not stale[service].startswith(PATH_HIT_PREFIX):
                continue
            stale[service] = reason
    return cleared


def _age(kept, after, now):
    """Seconds since the oldest commit after the render that touched a path in `kept`."""
    started = min(after[_hit_path(h)] for h in kept)
    return int((now if now is not None else time.time()) - started)


def _within_grace(kept, redated, after, grace_seconds, now):
    """Whether re-dating `kept` from the render puts it inside the grace window.

    Only when every kept hit is one the render's range touched: an `undigested_hits` hit from
    before the render keeps `compute_stale`'s date, which no render speaks to.
    """
    if grace_seconds <= 0 or not after or any(h not in redated for h in kept):
        return False
    return _age(kept, after, now) < grace_seconds


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
        renders_behind(records, renders, ref_sha, verdicts, repo_root)
        if ref_sha
        else {}
    )
    return apply_verdicts(
        stale,
        pending,
        verdicts,
        hits=hits,
        behind=behind,
        grace_seconds=grace_seconds,
        now=now,
    )
