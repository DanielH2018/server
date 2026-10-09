"""The Longhorn listing from B2 and its parser.

Split out of probe_lib/longhorn.py, which had grown to 630 lines. Everything here is about
Backblaze rather than about Longhorn's cluster state: paging `b2_list_file_names` under a
prefix through `lib.b2.B2Session`, and turning that listing into per-volume block and metadata
counts.

longhorn.py keeps the `b2-longhorn` and `b2-budget` subcommands that drive this.
`longhorn_budget.py` prices a retention prune from the same listing and `longhorn_cluster.py`
reads the live Volume/Backup/PV objects.
"""

# `probe_lib` is a namespace package under `scripts/`, so reaching a sibling by package name
# needs `scripts/` on sys.path — a module gets only its importer's path otherwise, and
# pyproject's `pythonpath` is a pytest setting. This has to sit ABOVE the imports below.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from diagnostics.probe_lib.core import DEFAULT_TIMEOUT

# Re-exported only: longhorn.py prints it in the `--dry-run` description of both subcommands.
from lib.b2 import AUTHORIZE_URL as B2_AUTHORIZE_URL  # noqa: F401
from lib.b2 import B2Error, B2Session, http_json

LONGHORN_PREFIX = "longhorn"


# DECIDED: b2_list_file_names is never a billable-bytes source; it sums current objects and
# under-reports what the cap measures. docs/observability-dashboards.md has the trap.
def b2_longhorn_lines(
    key_id,
    app_key,
    bucket,
    prefix=LONGHORN_PREFIX,
    _transport=http_json,
    _stats=None,
):
    """List the Longhorn prefix, returning `path;size` lines.

    The shape is rclone's `lsf --format ps --separator ;` verbatim, and the paths are made
    relative to the prefix the same way rclone's did — so parse_longhorn_listing below is
    unchanged and its tests still describe the real input. Leaving the paths absolute would
    match none of its patterns and report a healthy bucket as "no Longhorn backup objects".

    A B2 error on any call exits with B2's message rather than returning what was listed so
    far: a listing cut short by `transaction_cap_exceeded` would otherwise read as a store
    holding fewer blocks than it does.
    """
    strip = prefix.rstrip("/") + "/"
    try:
        session = B2Session(
            key_id, app_key, timeout=DEFAULT_TIMEOUT, transport=_transport
        )
        # A bucket-scoped application key already names its bucket; an account-wide one does
        # not and has to be looked up.
        if not session.bucket_id:
            session.lookup_bucket(bucket)
        files = session.list_files(strip)
    except B2Error as exc:
        raise SystemExit(f"B2 request failed: {exc}") from None

    lines = []
    for entry in files:
        name = entry.get("fileName", "")
        if name.startswith(strip):
            name = name[len(strip) :]
        lines.append(f"{name};{entry.get('contentLength', 0)}")
    # Each page is one b2_list_file_names, and the authorize that preceded them is itself
    # billable — both Class C. Reported through an out-param so the existing callers and their
    # tests keep the plain list return.
    if _stats is not None:
        _stats["class_c"] = session.class_c
        _stats["pages"] = session.list_calls
    return lines


def parse_longhorn_listing(lines):
    """Aggregate `rclone lsf --format ps` output per Longhorn volume.

    Longhorn lays a backup out as
    `backupstore/volumes/<aa>/<bb>/<volume>/{volume.cfg,backups/*.cfg,blocks/**/*.blk}`.
    The `.blk` files are the actual DATA; the `.cfg` files are only metadata, and that
    distinction is the entire point of this tool — a backup can be registered and report
    `Completed` in Longhorn while what actually landed in B2 is metadata describing blocks
    that are not there. Counting blocks is what makes "the data really is in B2" checkable.
    """
    vols = {}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        path, _, size = line.rpartition(";")
        if not path:
            continue
        parts = path.split("/")
        if "volumes" not in parts:
            continue
        i = parts.index("volumes")
        if len(parts) < i + 4:  # volumes/<aa>/<bb>/<volume>/...
            continue
        v = vols.setdefault(parts[i + 3], {"blocks": 0, "block_bytes": 0, "cfgs": 0})
        try:
            nbytes = int(size)
        except ValueError:
            nbytes = 0
        if path.endswith(".blk"):
            v["blocks"] += 1
            v["block_bytes"] += nbytes
        elif path.endswith(".cfg"):
            v["cfgs"] += 1
    return vols


def format_longhorn_summary(vols):
    """Render the per-volume table; non-zero exit if any volume has metadata but no data."""
    if not vols:
        return "no Longhorn backup objects found under the prefix", 1
    width = max(len(n) for n in vols)
    rows, bad = [], []
    for name in sorted(vols):
        v = vols[name]
        rows.append(
            "%-*s  %6d blocks  %8.1f MB  %3d cfg"
            % (width, name, v["blocks"], v["block_bytes"] / 1e6, v["cfgs"])
        )
        if v["blocks"] == 0:
            bad.append(name)
    out = "\n".join(rows)
    if bad:
        out += "\n\nNO DATA BLOCKS for: %s — metadata only, not restorable" % ", ".join(
            bad
        )
    return out, (1 if bad else 0)
