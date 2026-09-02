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
this module referring to the bare name and let the tests patch here. See probe_core's
docstring for why that matters.
"""

import json
import os
import re
from datetime import datetime, timezone

import probe_core as core
import probe_longhorn as longhorn
from probe_core import (
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


def record_b2_spend(tool, class_a=0, class_b=0, class_c=0, note="", _now=None):
    """Append one line of spend. Never raises: a ledger failure must not fail the real work."""
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
        with open(b2_ledger_path(), "a") as fh:
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
