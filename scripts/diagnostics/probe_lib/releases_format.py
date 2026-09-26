"""Renderers for `probe.py releases` -- the table, the cron-facing list and the Kuma push line.

Split from `releases.py` when that module reached the 600-line cap. Every function here is
pure: it takes what `releases.compute_stale` and `releases.missing_services` computed and
returns `(text, exit_code)`. `releases.py` re-exports them, so `pr.format_stale_kuma` in the
tests and `run_releases` keep one name for each.
"""

import json
import re

# `probe_lib` is a namespace package under `scripts/`, so reaching a sibling by package name
# needs `scripts/` on sys.path -- see the same bootstrap in releases.py.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from lib.repo_paths import REPO as REPO_ROOT


def format_records(records, merged, service=None, stale=None, release_dir=None):
    """Render the release table. Pure: returns (text, exit_code).

    `release_dir` is named in the no-records message only; `run_releases` passes
    `releases.RELEASE_DIR`, which stays there because `health.py` and the tests patch it.
    """
    if not records:
        return (
            "no release records found in {}\n"
            "Nothing has been deployed since the release stamp shipped -- deploy any k8s "
            "service to write the first one.".format(
                release_dir or "the release directory"
            ),
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
        "moved past this record under the service's own or a shared role, or an inventory "
        "key or shared macro its render reads (`probe.py releases --stale-only` for the "
        "reasons)."
    )
    return "\n".join(lines), (1 if unclean else 0)


def _pending_line(pending, grace_seconds):
    """The services inside the grace window, as one line; "" when none are."""
    if not pending:
        return ""
    names = ", ".join(
        f"{svc} ({age // 60} min ago)" for svc, age in sorted(pending.items())
    )
    return (
        f"{len(pending)} service(s) merged within the {grace_seconds // 60}-min grace, "
        f"not yet counted: {names}"
    )


def format_stale_only(stale, missing, pending=None, grace_seconds=0):
    """Render the cron-facing view: one line per stale or record-less service. Pure.

    A service inside the grace window is named on its own line but never moves the exit
    code: the line lets a reader of the `up` message see which deploy the window is
    waiting on.
    """
    lines = [f"{svc}: {reason}" for svc, reason in sorted(stale.items())]
    lines += [f"{svc}: no release record" for svc in missing]
    grace = _pending_line(pending, grace_seconds)
    if not lines:
        head = "0 service(s) stale; every known k8s service has a current record."
        return (f"{head} {grace}" if grace else head), 0
    if grace:
        lines.append(grace)
    return "\n".join(lines), 1


# The bridge pod ships `files/`, so the formatter lives there and probe.py reaches it the way
# the tests do: by putting that directory on sys.path. Bootstrapped here rather than at the
# top of the module because only `--kuma` needs it.
_BRIDGE_FILES = REPO_ROOT / "ansible/roles/k8s/monitor-bridge/files"

# Path prefixes a reason carries that add nothing inside a 900-char push message.
_REASON_NOISE = re.compile(r"ansible/(?:inventory|roles(?:/k8s)?)/")


def _kuma_reason(reason):
    return _REASON_NOISE.sub("", reason.removeprefix("changed since applied: "))


def format_stale_kuma(stale, missing, pending=None, grace_seconds=0):
    """The `--stale-only` verdict as one line grouped by reason, for the Kuma push. Pure.

    57 services carrying one identical reason are one group, not 57 lines (#2013); the group
    lists the names once. Shares the exit code contract with `format_stale_only`, including
    the pending line: named on an `up`, never counted. A DOWN message omits it, because that
    message is capped and the stale services are the part a reader needs.
    """
    items = {svc: _kuma_reason(reason) for svc, reason in stale.items()}
    items.update(dict.fromkeys(missing, "no release record"))
    if not items:
        head = "0 services stale; every known k8s service has a current record."
        grace = _pending_line(pending, grace_seconds)
        return (f"{head} {grace}" if grace else head), 0
    if str(_BRIDGE_FILES) not in _sys.path:
        _sys.path.insert(0, str(_BRIDGE_FILES))
    from bridge import msgfmt

    return (
        msgfmt.format_down(
            "service", "stale", items, details="probe.py releases --stale-only"
        ),
        1,
    )


def write_counted_names(path, stale, missing):
    """Write the services the verdict COUNTS, sorted, one per line, to `path`.

    release-staleness-check.sh compares this set against its previous DOWN run to re-alert
    when the set changes while the tile stays DOWN (#2378). The `--kuma` line cannot stand in
    for it: it is grouped and capped at 900 chars. Services inside the grace window are left
    out, because they do not move the verdict either. An empty set writes an empty file, and
    a `path` of None (no `--names-out`) writes nothing.
    """
    if path is None:
        return
    names = sorted(set(stale) | set(missing))
    _Path(path).write_text("".join(f"{n}\n" for n in names))
