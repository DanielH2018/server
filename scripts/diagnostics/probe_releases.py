"""`probe.py releases` -- which commit produced the manifests each k8s service is running.

WHY THIS EXISTS. `roles/k8s/manifests/tasks/release_stamp.yml` writes a record per service after
every apply, naming the commit that rendered the bytes. Without a reader that record is a state
nobody can see, which is half a feature. This is the reader.

WHAT IT ANSWERS THAT NOTHING ELSE DOES. `kubectl` reports what is running; git reports what is
committed; neither knows which commit produced the running manifests. `deploy.sh` renders from
whatever tree it is invoked in, so those two can disagree without anything going red -- a
worktree 48 commits behind master reverted claude-otel for nine minutes on 2026-08-19 and the
only symptom was a scrape-target count moving.

THE TWO FLAGS ARE THE POINT. `dirty` means the tree had uncommitted tracked changes, so no
commit reproduces those bytes. `unmerged` means the commit is not an ancestor of
origin/master -- a service running code that never landed. Both are normal mid-slice and both
are alarming a week later, which is why they are reported rather than judged.

Exit codes: 0 when every record is clean, 1 when any service is dirty or unmerged, 2 when no
records exist at all (nothing has been deployed since the stamp shipped).
"""

import json
import subprocess
from pathlib import Path

# `core.<name>` for anything the tests monkeypatch -- binding those into this module's globals
# with a `from probe_core import ...` would take a snapshot the patch never reaches.
import probe_core as core  # noqa: F401  (kept for the monkeypatch convention above)

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys

_sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Mirrors manifests_release_dir in roles/k8s/manifests/defaults/main.yml. A mismatch makes this
# reader silently report "no records", so scripts/diagnostics/tests/test_probe_releases.py asserts the
# two agree rather than trusting the comment.
RELEASE_DIR = Path("/var/lib/homelab/k8s-releases.d")

from lib.repo_paths import REPO as REPO_ROOT  # noqa: E402


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
                capture_output=True,
                timeout=10,
            ).returncode
        except OSError, subprocess.SubprocessError:
            continue
        if rc == 0:
            merged.add(commit)
    return merged


def format_records(records, merged, service=None):
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
        f"{len(records)} service(s); {unclean} carrying a flag. "
        "dirty = no commit reproduces those bytes; unmerged = not an ancestor of origin/master."
    )
    return "\n".join(lines), (1 if unclean else 0)


def run_releases(ns):
    records = load_records(previous=getattr(ns, "previous", False))
    if getattr(ns, "json", False):
        print(json.dumps(records, indent=2))
        return 0
    merged = merged_commits(r.get("commit") for r in records)
    text, code = format_records(records, merged, service=getattr(ns, "service", None))
    print(text)
    return code
