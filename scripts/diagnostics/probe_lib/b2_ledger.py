"""The B2 spend ledger: what maintenance tools spent, since B2 publishes no usage API.

Backs the `b2-spend` and `b2-record` subcommands. B2 charges per transaction class and
reports the totals nowhere an API can reach: the Native API has no usage operation, and the
per-class Usage Reports are Partner-tier. Backup spend is recoverable from Longhorn's logs
(see BACKUP_BLOCKS_RE), but MAINTENANCE spend — the drains, inventories and verification
listings an operator runs by hand — leaves no trace at all once the terminal scrolls. On
2026-08-17 that was most of a 2,000-transaction day and had to be reconstructed from memory,
badly. Anything that talks to B2 records what it spent here, so the controllable half of the
bill stops being guesswork.

Patched in tests via the module attribute (`ledger.B2_LEDGER_DIR`), so keep callers inside
this module referring to the bare name and let the tests patch here. See core's
docstring for why that matters.
"""

import json
import os
import re
from datetime import datetime, timezone

# `probe_lib` is a namespace package under `scripts/`, so reaching a sibling by package name
# needs `scripts/` on sys.path — a module gets only its importer's path otherwise, and
# pyproject's `pythonpath` is a pytest setting. This has to sit ABOVE the imports below.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from diagnostics.probe_lib import core
from diagnostics.probe_lib import longhorn
from diagnostics.probe_lib.core import (
    _CHICAGO,
    _rows_from_loki,
    loki_endpoint,
    loki_query_url,
    parse_duration_seconds,
)

B2_LEDGER_DIR = os.path.expanduser("~/.local/state/homelab/b2-ledger")


def b2_ledger_path(day=None):
    """One TSV per UTC day — the day B2's own counters reset on, not the local day."""
    day = day or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return os.path.join(B2_LEDGER_DIR, f"{day}.tsv")


def record_b2_spend(
    tool, class_a=0, class_b=0, class_c=0, note="", _now=None, day=None
):
    """Append one line of spend. Never raises: a ledger failure must not fail the real work.

    `day` charges the spend to a UTC day other than today's. A tool that reports its own spend
    as it happens never needs it; one that accounts for spend AFTER the fact does, because B2's
    caps reset per UTC day — a deletion at 23:50 UTC discovered by the next morning's run
    belongs to the cap it actually consumed, not to the cap the run happens to fall under.
    """
    try:
        os.makedirs(B2_LEDGER_DIR, exist_ok=True)
        stamp = (_now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
        line = "\t".join(
            [
                stamp,
                tool,
                str(class_a),
                str(class_b),
                str(class_c),
                note.replace("\t", " "),
            ]
        )
        with open(b2_ledger_path(day), "a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def parse_b2_ledger(lines):
    """TSV lines -> per-tool {class_a, class_b, class_c, runs}. Malformed lines are skipped."""
    tools = {}
    for line in lines:
        parts = line.rstrip("\n").split("\t")
        if len(parts) < 5:
            continue
        try:
            a, b, c = int(parts[2]), int(parts[3]), int(parts[4])
        except ValueError:
            continue
        entry = tools.setdefault(
            parts[1], {"runs": 0, "class_a": 0, "class_b": 0, "class_c": 0}
        )
        entry["runs"] += 1
        entry["class_a"] += a
        entry["class_b"] += b
        entry["class_c"] += c
    return tools


def read_b2_ledger(day=None):
    try:
        with open(b2_ledger_path(day)) as fh:
            return parse_b2_ledger(fh.readlines())
    except OSError:
        return {}


# Longhorn logs one of these per backup, naming the delta it processed:
#   "Created snapshot changed blocks: 46 mappings, 46 blocks and 45 new blocks"
# `blocks` is the delta it walks, and it issues one HeadObject per block to decide whether the
# block is already in the store — so that count IS the backup's Class B transaction cost. `new`
# is what it then uploaded, which is Class A and unmetered. B2 publishes no usage endpoint (its
# per-class counters are Partner-tier only), so this line is the closest thing to a meter the
# backup plane has, and it costs nothing because the logs are already in Loki.
BACKUP_BLOCKS_RE = re.compile(
    r"Created snapshot changed blocks: \d+ mappings, (\d+) blocks and (\d+) new blocks"
)
# The replica that emitted the line, e.g. "[pvc-1c0e18da-...-r-9d333575] time=..."
BACKUP_VOLUME_RE = re.compile(r"\[(pvc-[0-9a-f-]{36})-r-[0-9a-f]+\]")
SPEND_LOGQL = '{namespace="longhorn-system"} |= "Created snapshot changed blocks"'


def parse_backup_spend(rows):
    """[(ns_timestamp, line)] -> per-volume {backups, blocks, new_blocks}.

    Lines whose replica prefix was trimmed by the log pipeline still count toward the totals;
    they are attributed to "unattributed" rather than dropped, because losing them would
    understate spend and understating is the failure mode that matters here.
    """
    vols = {}
    for _, line in rows:
        m = BACKUP_BLOCKS_RE.search(line)
        if not m:
            continue
        v = BACKUP_VOLUME_RE.search(line)
        name = v.group(1) if v else "unattributed"
        entry = vols.setdefault(name, {"backups": 0, "blocks": 0, "new_blocks": 0})
        entry["backups"] += 1
        entry["blocks"] += int(m.group(1))
        entry["new_blocks"] += int(m.group(2))
    return vols


def format_backup_spend(vols, window, names=None, ledger=None):
    """Render measured backup spend alongside recorded maintenance spend.

    Never exits non-zero: this is a meter, not a gate. The two halves are printed separately and
    deliberately NOT summed — the backup figure spans --since while the ledger covers the UTC day
    B2's counters reset on, so a combined total would match neither window.
    """
    names = names or {}
    rows = []
    if vols:
        rows.append("%-24s %8s %8s %10s" % ("PVC", "BACKUPS", "BLOCKS", "UPLOADED"))
        for vol in sorted(vols, key=lambda k: -vols[k]["blocks"]):
            v = vols[vol]
            rows.append(
                "%-24s %8d %8d %10d"
                % (names.get(vol, vol)[:24], v["backups"], v["blocks"], v["new_blocks"])
            )
        rows.append("")
        rows.append(
            "backups over %s: %d Class B measured, %d blocks uploaded (Class A, unmetered)"
            % (
                window,
                sum(v["blocks"] for v in vols.values()),
                sum(v["new_blocks"] for v in vols.values()),
            )
        )
    else:
        rows.append(
            f"no backups logged in the last {window} — widen --since, or nothing ran"
        )

    ledger = ledger or {}
    rows.append("")
    if ledger:
        rows.append("maintenance recorded today (UTC), from the ledger:")
        for tool in sorted(ledger, key=lambda k: -ledger[k]["class_c"]):
            t = ledger[tool]
            rows.append(
                "  %-22s %3d run(s) %6d Class B %6d Class C"
                % (tool[:22], t["runs"], t["class_b"], t["class_c"])
            )
        rows.append(
            "  %-22s %10d Class B %6d Class C"
            % (
                "TOTAL",
                sum(t["class_b"] for t in ledger.values()),
                sum(t["class_c"] for t in ledger.values()),
            )
        )
    else:
        rows.append(
            "no maintenance recorded today — nothing has written the ledger yet"
        )

    rows.append("")
    rows.append(
        "Still unrecorded: Longhorn's own metadata reads (Backup-CR pulls, target syncs) and "
        "the monitor's B2 probes. B2 publishes no counter to reconcile against, so the console's "
        "Caps & Alerts page remains the only ground truth."
    )
    return "\n".join(rows)


def run_b2_spend(ns):
    """Measured Class B spend from Longhorn's own logs, over --since.

    Costs nothing against B2 — it reads Loki, not the backup store. This is the counterpart to
    b2-budget: that one PROJECTS Class C from block counts, this one MEASURES Class B from what
    the backups actually did.
    """
    seconds = parse_duration_seconds(ns.since)
    end_s = datetime.now(_CHICAGO).timestamp()
    base, pin = loki_endpoint()
    url = loki_query_url(
        base,
        SPEND_LOGQL,
        ns.limit,
        start=int((end_s - seconds) * 1e9),
        end=int(end_s * 1e9),
        direction="forward",
    )
    if ns.dry_run:
        return core.print_dry_run(url, resolve=pin)
    rows = _rows_from_loki(json.loads(core.fetch(url, resolve=pin)))
    # Reads Loki, not B2 — nothing to record for this command itself.
    print(
        format_backup_spend(
            parse_backup_spend(rows), ns.since, longhorn.pvc_names(), read_b2_ledger()
        )
    )
    return 0


def run_b2_record(ns):
    """Record a tool's B2 spend in today's ledger.

    Exists so scripts outside this repo — the one-shot drains and inventories an operator writes
    during an incident — can contribute to the same tally instead of scrolling past in a terminal
    and being reconstructed from memory afterwards.
    """
    record_b2_spend(ns.tool, ns.class_a, ns.class_b, ns.class_c, ns.note)
    print(
        "recorded %s: %d Class A, %d Class B, %d Class C -> %s"
        % (ns.tool, ns.class_a, ns.class_b, ns.class_c, b2_ledger_path())
    )
    return 0


# --- b2-deletions: charge a deletion that already happened -------------------------------------

# Longhorn logs a pair per deleted backup — `Start deleting backup` (backups.go:302) and
# `Complete deleting backup` (:310) — both naming the target URL, the backup ID and the volume:
#
#   msg="Complete deleting backup s3://bucket@region/longhorn?backup=backup-0ab05217dd5a4501
#        &volume=pvc-c2ca0afb-74f0-4507-a29a-3cf40aac175d"
#
# Anchor on Complete, never Start. Counting both doubles every deletion, the pair straddles a
# window edge routinely (measured 2026-09-03: one pair spanned 03:30:52 to 03:33:39), and
# Complete is the line that means the block-tree walk actually spent the transactions.
DELETE_COMPLETE_RE = re.compile(
    r"Complete deleting backup (?P<target>s3://[^\s?\"]+)\?"
    r"backup=(?P<backup>[A-Za-z0-9_-]+)&volume=(?P<volume>pvc-[0-9a-f-]{36})"
)
DELETIONS_LOGQL = '{namespace="longhorn-system"} |= "Complete deleting backup"'
# Written by `b2-budget`, read here. Lives beside the ledger so a test that patches
# B2_LEDGER_DIR relocates both.
PRUNE_SNAPSHOT_NAME = "prune-costs.json"


def prune_snapshot_path():
    return os.path.join(B2_LEDGER_DIR, PRUNE_SNAPSHOT_NAME)


def write_prune_snapshot(vols, _now=None):
    """Persist `{volume: prune}` plus when it was measured. Never raises, like record_b2_spend."""
    try:
        os.makedirs(B2_LEDGER_DIR, exist_ok=True)
        stamp = (_now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
        payload = {
            "measured_at": stamp,
            "volumes": {vol: v["prune"] for vol, v in vols.items() if "prune" in v},
        }
        with open(prune_snapshot_path(), "w") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
    except OSError:
        pass


def read_prune_snapshot():
    """-> (measured_at, {volume: prune}). ({}, "") when nothing has written one yet."""
    try:
        with open(prune_snapshot_path()) as fh:
            payload = json.load(fh)
    except OSError, json.JSONDecodeError, ValueError:
        return "", {}
    volumes = payload.get("volumes")
    if not isinstance(volumes, dict):
        return "", {}
    return payload.get("measured_at", ""), volumes


def parse_backup_deletions(rows, target_url):
    """[(ns_timestamp, line)] -> [{stamp, backup, volume}] for deletions against `target_url`.

    Filtering on the target is the whole reason this takes an argument. The cluster deletes
    against two backup stores and only one is B2; an R2 deletion charged here would inflate the
    ledger against a daily cap that does not govern R2 at all. An empty `target_url` means the
    caller could not establish which store is B2, so nothing is claimed.
    """
    if not target_url:
        return []
    seen, out = set(), []
    for ts, line in rows:
        m = DELETE_COMPLETE_RE.search(line)
        if not m or m.group("target") != target_url:
            continue
        backup = m.group("backup")
        # Loki can return the same line twice across overlapping chunks; a backup ID is deleted
        # exactly once, so the ID is a safe key within one window as well as across runs.
        if backup in seen:
            continue
        seen.add(backup)
        out.append({"stamp": ts, "backup": backup, "volume": m.group("volume")})
    return out


LEDGER_DELETIONS_TOOL = "b2-deletions"
_LEDGER_BACKUP_ID_RE = re.compile(r"\b(backup-[A-Za-z0-9_-]+)\b")


def recorded_deletion_ids(days):
    """Backup IDs this tool has already charged, read from the raw ledger lines of `days`.

    `read_b2_ledger` cannot answer this: it collapses to per-tool totals and drops the note the
    ID lives in. Reading several days matters because a deletion just before 00:00 UTC is
    recorded in one day's file and re-seen by the next run, whose file is a different one.
    """
    ids = set()
    for day in days:
        try:
            with open(b2_ledger_path(day)) as fh:
                lines = fh.readlines()
        except OSError:
            continue
        for line in lines:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 6 or parts[1] != LEDGER_DELETIONS_TOOL:
                continue
            m = _LEDGER_BACKUP_ID_RE.search(parts[5])
            if m:
                ids.add(m.group(1))
    return ids


def deletion_utc_day(ns_timestamp):
    """The UTC day a Loki row's nanosecond timestamp falls on, as `YYYY-MM-DD`."""
    return datetime.fromtimestamp(ns_timestamp / 1e9, timezone.utc).strftime("%Y-%m-%d")


def ledger_days_spanning(seconds, _now=None):
    """Every UTC ledger day a `--since <seconds>` window can have written into, newest first."""
    end = (_now or datetime.now(timezone.utc)).timestamp()
    days, t = [], end
    while t > end - seconds - 86400:
        day = datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")
        if day not in days:
            days.append(day)
        t -= 86400
    return days


def price_deletions(deletions, prices):
    """-> (priced, unpriced). A deletion's Class C cost is its volume's whole-block-tree walk.

    `DeleteDeltaBlockBackup` runs `getBlockNamesForVolume`, one delimited ListObjects per
    `blocks/` directory, so the price is `1 + lv1 + lv2` — the figure `parse_backup_budget`
    already computes. A volume absent from the snapshot is returned UNPRICED rather than charged
    zero: a silent zero is indistinguishable from a free deletion, and understating is the
    failure mode this ledger exists to prevent.
    """
    priced, unpriced = [], []
    for d in deletions:
        cost = prices.get(d["volume"])
        if cost is None:
            unpriced.append(d)
        else:
            priced.append(dict(d, class_c=cost))
    return priced, unpriced


def format_backup_deletions(priced, unpriced, skipped, window, measured_at, names=None):
    """Render what was charged, what could not be, and how old the price is.

    Exit 1 when a deletion went unpriced. That is the one state an operator has to act on — the
    transactions were spent, the tree that would have priced them is gone, and no later run can
    recover the figure. Everything else here is a meter.
    """
    names = names or {}
    rows = []
    if priced:
        rows.append("%-24s %-26s %8s" % ("PVC", "BACKUP", "CLASS C"))
        for d in sorted(priced, key=lambda x: -x["class_c"]):
            rows.append(
                "%-24s %-26s %8d"
                % (names.get(d["volume"], d["volume"])[:24], d["backup"], d["class_c"])
            )
        rows.append("")
        rows.append(
            "charged %d deletion(s) over %s: %d Class C, priced from the %s block-tree listing"
            % (
                len(priced),
                window,
                sum(d["class_c"] for d in priced),
                measured_at or "(unknown date)",
            )
        )
        rows.append(
            "  That price is a MODEL, not a measurement: the tree each deletion walked is gone, "
            "and a listing taken after the deletion is smaller than the one it walked. The "
            "figure is therefore a floor. Run b2-budget before the prune window to keep it tight."
        )
    else:
        rows.append(f"no new B2 backup deletions in the last {window}")
    if skipped:
        rows.append(
            "%d deletion(s) already in the ledger from an earlier run — not re-charged"
            % skipped
        )
    if unpriced:
        rows.append("")
        rows.append(
            "UNPRICED — these deletions spent Class C that cannot now be recovered:"
        )
        for d in unpriced:
            rows.append(
                "  %-24s %s" % (names.get(d["volume"], d["volume"])[:24], d["backup"])
            )
        rows.append(
            "  Their volume is absent from the block-tree snapshot, so nothing prices them. "
            "Each is recorded at zero with UNPRICED in the note — the ledger says the deletion "
            "happened and that its cost is unrecoverable. Run `probe.py b2-budget` to write a "
            "snapshot; every later deletion of these volumes is then chargeable."
        )
    return "\n".join(rows), (1 if unpriced else 0)


def run_b2_deletions(ns):
    """Charge Longhorn's completed backup deletions to today's ledger, after the fact.

    The ledger's gap until 2026-09-03: `record_b2_spend` was called only from the two read-only
    listing commands, so the one class of operation it exists to capture — a deletion, which
    walks a whole block tree at ~1.28 Class C per stored block — wrote no line at all. Deriving
    it from `longhorn-manager`'s own logs needs no cooperation from whoever ran the deletion,
    which is the property a `b2-record` call inside each drop playbook would still lack.

    Reads Loki and the Kubernetes API only. It spends nothing on B2, so it is safe on a timer.
    """
    seconds = parse_duration_seconds(ns.since)
    end_s = datetime.now(_CHICAGO).timestamp()
    base, pin = loki_endpoint()
    url = loki_query_url(
        base,
        DELETIONS_LOGQL,
        ns.limit,
        start=int((end_s - seconds) * 1e9),
        end=int(end_s * 1e9),
        direction="forward",
    )
    if ns.dry_run:
        return core.print_dry_run(url, resolve=pin)

    target = ns.target_url or longhorn.backup_target_url()
    if not target:
        # Disarmed or absent. Declining beats matching everything: the R2 deletions in the same
        # log stream would be charged to B2's cap.
        print(
            "the B2 BackupTarget has no URL — disarmed, or the CR is gone. Nothing classified; "
            "pass --target-url to charge deletions against a store this cannot read."
        )
        return 0

    rows = _rows_from_loki(json.loads(core.fetch(url, resolve=pin)))
    deletions = parse_backup_deletions(rows, target)
    # Every ledger day the window can have written into. A deletion is charged to the UTC day it
    # happened on, so a `--since` reaching back past midnight has already recorded into an
    # earlier file — reading only today's would re-charge everything from before 00:00 UTC.
    already = recorded_deletion_ids(ledger_days_spanning(seconds))
    fresh = [d for d in deletions if d["backup"] not in already]
    measured_at, prices = read_prune_snapshot()
    priced, unpriced = price_deletions(fresh, prices)

    if not ns.no_record:
        for d in priced:
            record_b2_spend(
                LEDGER_DELETIONS_TOOL,
                class_c=d["class_c"],
                note="%s vol=%s priced from %s listing"
                % (d["backup"], d["volume"], measured_at or "unknown"),
                day=deletion_utc_day(d["stamp"]),
            )
        # An unpriced deletion gets a line too, at zero, with UNPRICED in the note. The zero
        # understates the day's spend, which this module otherwise treats as the failure mode
        # that matters — but the alternative understates it by exactly as much AND leaves no
        # trace that a deletion happened at all, which is the gap this whole command closes.
        # Recording it also stops a later run charging it against a snapshot taken after the
        # fact, which would be a fabricated number rather than a missing one.
        for d in unpriced:
            record_b2_spend(
                LEDGER_DELETIONS_TOOL,
                class_c=0,
                note="%s vol=%s UNPRICED — no block-tree snapshot covered this volume"
                % (d["backup"], d["volume"]),
                day=deletion_utc_day(d["stamp"]),
            )
    text, code = format_backup_deletions(
        priced,
        unpriced,
        len(deletions) - len(fresh),
        ns.since,
        measured_at,
        longhorn.pvc_names(),
    )
    print(text)
    return code
