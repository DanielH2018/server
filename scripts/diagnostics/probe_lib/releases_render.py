"""Clear a `--stale-only` path hit when a render record shows the applied bytes are current.

WHY THIS EXISTS (#2586). `releases.compute_stale` decides staleness from the paths a merge
touched, and a path is a proxy: five narrowings exist because a path moved while the rendered
bytes did not. A render-mode dry run (`-e k8s_dry_run=true -e manifests_render_record=true`)
writes `k8s-renders.d/<service>.json`, whose `manifests_digest` comes from the same
`release_digest.yml` as the release record's. When the two digests match, the bytes the apply
wrote are the bytes the ref renders, and a path hit is a false positive.

A MATCH IS NOT PROOF ON ITS OWN. `manifests_digest` excludes secret manifests by design, and
uptime-kuma's `static-monitors.yaml` is one: a monitor added there left the digest identical
while the path check correctly read it stale (2026-09-25). So a service with secret manifests
also needs the same names on both records and a matching `secret_digest`, an HMAC under a
host-local key that the operator approved on 2026-09-26 (#2574). A record without that field
keeps the path verdict, so the fleet converges one redeploy at a time. A render record is evidence only when its
`commit` IS the ref, its tree was clean, and its `host` is the release record's host, because
a digest from another commit, a dirty tree or another host's vars names different bytes.
Every refusal keeps the path verdict; nothing here can make a service stale.

Only path hits are cleared. "commit unknown to this checkout" is a different doubt -- nobody
can say where the applied bytes came from -- and stays reported.
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


def _secrets_match(release, render):
    """Whether the two records agree on the secret manifests, which `manifests_digest` excludes.

    Both lists empty agrees trivially. Otherwise the names must be identical and both records
    must carry the same non-empty `secret_digest` (#2574). A record written before that field,
    or one whose host key was unreadable ('' by design), falls back to the path verdict.
    """
    names = release.get("secret_manifests")
    if names is None or names != render.get("secret_manifests"):
        return False
    if names == []:
        return True
    digest = release.get("secret_digest")
    return bool(digest) and digest == render.get("secret_digest")


def render_proves_current(release, render, ref_sha):
    """Whether `render` shows `release`'s applied bytes are what `ref_sha` renders."""
    if not release or not render or not ref_sha:
        return False
    if render.get("commit") != ref_sha or render.get("tree_dirty") is not False:
        return False
    # A release record written before #2532 carries no host. That is "unknown", never a match.
    if not release.get("host") or release.get("host") != render.get("host"):
        return False
    if not _secrets_match(release, render):
        return False
    digest = release.get("manifests_digest")
    return bool(digest) and digest == render.get("manifests_digest")


def clear_matched(stale, pending, records, renders, ref_sha):
    """Drop every service a render record proves current from `stale` and `pending`, in place.

    Returns the sorted names cleared from `stale`. A service inside the grace window is
    dropped from `pending` too, so it does not read as a merge still waiting on its deploy.
    """
    by_service = {r.get("service"): r for r in records if "error" not in r}
    proven = {
        svc
        for svc in set(stale) | set(pending or {})
        if render_proves_current(by_service.get(svc), renders.get(svc), ref_sha)
    }
    cleared = sorted(
        svc for svc in proven if svc in stale and stale[svc].startswith(PATH_HIT_PREFIX)
    )
    for svc in cleared:
        del stale[svc]
    for svc in proven & set(pending or {}):
        del pending[svc]
    return cleared


def apply_renders(
    stale,
    records,
    pending=None,
    repo_root=REPO_ROOT,
    ref="origin/master",
    render_dir=None,
):
    """`clear_matched` against the render records in `render_dir` and `ref` resolved in `repo_root`."""
    renders = load_renders(render_dir or RENDER_DIR)
    if not renders:
        return []
    return clear_matched(stale, pending, records, renders, resolve_ref(ref, repo_root))
