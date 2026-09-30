"""The render digest, which is `probe.py releases`' primary answer on staleness (#3046).

WHY THIS EXISTS (#2586). `releases.compute_stale` decides staleness from the paths a merge
touched, and a path is a proxy: five narrowings exist because a path moved while the rendered
bytes did not. A render-mode dry run (`-e k8s_dry_run=true -e manifests_render_record=true`)
writes `k8s-renders.d/<service>.json`, whose `manifests_digest` comes from the same
`release_digest.yml` as the release record's. The two digests therefore identify the same
bytes, and comparing them answers all five narrowings at once.

THE LADDER. `digest_verdict` returns one of three answers for a service, and the first two
outrank the path check:

  * `CURRENT` -- the digests agree, so the bytes the apply wrote are the bytes the ref renders.
    Every path hit is a false positive and is dropped.
  * `DRIFTED` -- the record is trustworthy and the digests disagree, so the applied bytes are
    not what the ref renders. The service is stale for that reason, WHETHER OR NOT a path
    moved. This is what #3046 inverted: until then a digest could only clear a path hit, so a
    service the path narrowing read clean could drift unseen.
  * `UNKNOWN` -- no record, or one that proves nothing (below). The path verdict stands
    unchanged, which is the whole of the pre-#3046 behaviour.

WHAT MAKES A RECORD PROVE NOTHING. A digest is evidence only when the render record's `commit`
IS the ref, its tree was clean, and its `host` is the release record's, because a digest from
another commit, a dirty tree or another host's vars names different bytes. `manifests_digest`
also excludes secret manifests by design, and uptime-kuma's `static-monitors.yaml` is one: a
monitor added there left the digest identical while the path check correctly read it stale
(2026-09-25). The `secret_digest` HMAC covers that (#2574), so a service with secret manifests
needs that field on BOTH records; one written before it, or one whose host key was unreadable
('' by design), falls back to the path verdict and the fleet converges one redeploy at a time.

TWO THINGS A DRIFTED VERDICT DOES NOT OVERRIDE. A service inside `compute_stale`'s grace window
keeps waiting: the window exists because a landing's own deploy is still in flight (code-server,
2026-09-21), and a digest cannot tell that apart from drift. And "commit unknown to this
checkout" is a doubt about PROVENANCE -- nobody can say where the applied bytes came from --
which no digest speaks to, so it stays reported as it was.

THE EVALUATION ORDER IS NOT THE PRECEDENCE ORDER. `run_releases` runs `compute_stale` first and
then applies the verdicts over its result. The digest still decides -- it replaces or creates
the reason for every service it can answer for -- but computing it first would mean skipping
the path work for a `CURRENT` service, and that work is where the unknown-commit and grace
answers come from.
"""

import json
import subprocess
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
    # A release record written before #2532 carries no host. That is "unknown", never a match.
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


def apply_verdicts(stale, pending, verdicts):
    """Let the digest answer override the path verdict in `stale` and `pending`, in place.

    Returns the sorted names a CURRENT verdict cleared from `stale`.

    A CURRENT service is dropped from `pending` too, so it does not read as a merge still
    waiting on its deploy. A DRIFTED one is left alone where the grace window or the
    unknown-commit doubt already answers for it, per the module docstring.
    """
    cleared = []
    for service, (verdict, reason) in sorted(verdicts.items()):
        if verdict == CURRENT:
            if service in stale and stale[service].startswith(PATH_HIT_PREFIX):
                del stale[service]
                cleared.append(service)
            if pending and service in pending:
                del pending[service]
        elif verdict == DRIFTED:
            if pending and service in pending:
                continue
            if service in stale and not stale[service].startswith(PATH_HIT_PREFIX):
                continue
            stale[service] = reason
    return cleared


def apply_digest_verdicts(
    stale,
    records,
    pending=None,
    repo_root=REPO_ROOT,
    ref="origin/master",
    render_dir=None,
):
    """`apply_verdicts` for the render records in `render_dir` and `ref` resolved in `repo_root`."""
    renders = load_renders(render_dir or RENDER_DIR)
    if not renders:
        return []
    return apply_verdicts(
        stale, pending, verdicts_for(records, renders, resolve_ref(ref, repo_root))
    )
