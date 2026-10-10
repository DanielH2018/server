#!/usr/bin/env python3
"""Prune the LAPI bouncer rows every past Traefik pod IP left behind (#2762).

LAPI authenticates a bouncer by the pair (API-key hash, client IP). A key it knows arriving
from an IP it has no row for gets a new auto-created row, `<base>@<ip>`, and the old row is
never touched again (`authPlain`, pkg/apiserver/middlewares/v1/api_key.go, v1.8.1). Every
Traefik restart gets a new pod IP, so `k8straefik` had grown to 80 rows by 2026-09-27, each a
valid identity for the same key, and a reader of the base row's `last_pull` saw 2026-08-09.

`cscli bouncers prune --duration` is the only cscli path that removes one auto-created row:
`cscli bouncers delete <name>@<ip>` warns "auto-created and cannot be deleted" and exits 0,
and deleting the parent deletes every child with it. Prune deletes every bouncer, base row
included, whose `last_pull` (or `created_at`, when it never pulled) is older than the
duration. That is safe for exactly one reason and this module exists to check it: LAPI answers
403 only when NO row carries the key. While one row does, a pull from any IP re-creates
`k8straefik@<ip>` on the spot with a zero stream cursor, so the bouncer gets a full resync
rather than an error. A prune run while no row has pulled inside the window, such as during a
Traefik outage longer than the window, would delete the last row, and the edge would fail its
stream until `updateMaxFailure` turned that into a blanket 403. So a run whose prune leaves no
`k8straefik` row pulled inside `WINDOW - MARGIN` refuses, and says why.

The base row goes too, since it last pulled in August. Nothing is lost: the image entrypoint
re-adds `k8straefik` from `BOUNCER_KEY_k8straefik` at every engine start when the name is
missing, and `api_key` carries no unique constraint to refuse it. The retired
`dockertraefik`, which never pulled, is pruned on the first run.

Runs from the cron wrapper `crowdsec-prune-bouncers.sh` as root on daniel-box. The wrapper owns
the syslog line; this module owns the decision, and its last stdout line is the summary. The
functions above `main` are pure and are what `tests/test_bouncer_prune.py` exercises.

Usage:

    uv run --no-project --python <pin> /opt/crowdsec-bouncer-prune/bouncer_prune.py

Takes no arguments and reads no environment variables. It runs `cscli` through
`k3s kubectl exec deploy/crowdsec` in the homelab namespace, so it needs root on daniel-box.
Exit 0 means it pruned or had nothing to prune. Exit 1 means it refused or a cscli call failed,
and the last stdout line says why. `-h` and `--help` print this text and exit 0.
"""

import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

EDGE_BOUNCER = "k8straefik"
NAMESPACE = "homelab"
K3S = "/usr/local/bin/k3s"
# The plugin pulls every 60s (`updateIntervalSeconds`), and the longest silent gap measured was
# the metrics-ticker stall #2752 removed: 1200s. An hour is three times that, and with an hourly
# cron a restarted pod's old row is gone within two hours of its last pull.
WINDOW = timedelta(minutes=60)
# The survivor must have pulled this much inside the window, so that the seconds between the
# list and the prune cannot age it out.
MARGIN = timedelta(minutes=10)
# `logger` truncates a message at 1 KiB, and the first run deletes about 80 rows.
LISTED = 10


def parse_time(raw):
    """A cscli JSON timestamp (Go RFC3339Nano) as an aware datetime, or None for a null.

    `fromisoformat` takes the `Z` and truncates nanoseconds to microseconds on Python 3.11+.
    """
    return datetime.fromisoformat(raw) if raw else None


def last_seen(row):
    """What `QueryBouncersInactiveSince` compares: `last_pull`, else `created_at`."""
    return parse_time(row.get("last_pull")) or parse_time(row.get("created_at"))


def base_name(name):
    """`k8straefik@10.42.0.207` -> `k8straefik`; LAPI's own `baseBouncerName` strips the same."""
    return name.split("@", 1)[0]


@dataclass
class Plan:
    prune: list
    refused: str = ""


def report(before, after, edge=EDGE_BOUNCER):
    """The summary line, read from the list taken AFTER the prune rather than from the plan.

    It names the rows that actually went and the edge rows left, with the oldest `last_pull`
    among them: the number to compare with the live Traefik pod's start (#2762's Verify-by).
    """
    gone = sorted({r["name"] for r in before} - {r["name"] for r in after})
    left = [r for r in after if base_name(r["name"]) == edge]
    pulled = [r for r in left if r.get("last_pull")]
    oldest = min(pulled, key=lambda r: parse_time(r["last_pull"]), default=None)
    oldest = oldest["last_pull"] if oldest else "none"
    state = f"{len(left)} {edge} rows left, oldest last pull {oldest}"
    if not gone:
        return f"nothing pruned; {state}"
    more = f" and {len(gone) - LISTED} more" if len(gone) > LISTED else ""
    return f"pruned {len(gone)} rows: {','.join(gone[:LISTED])}{more}; {state}"


def plan(rows, now, window=WINDOW, margin=MARGIN, edge=EDGE_BOUNCER):
    """What `cscli bouncers prune -d <window>` would delete from `rows`, and whether that is safe."""
    cutoff = now - window
    prune = sorted(r["name"] for r in rows if last_seen(r) < cutoff)
    edge_rows = [
        r
        for r in rows
        if base_name(r["name"]) == edge and parse_time(r.get("last_pull"))
    ]
    survivor = max(edge_rows, key=lambda r: parse_time(r["last_pull"]), default=None)
    if survivor is None or parse_time(survivor["last_pull"]) < cutoff + margin:
        newest = survivor["last_pull"] if survivor else "never"
        return Plan(
            prune,
            f"refusing to prune: no {edge} row pulled in the last "
            f"{int((window - margin).total_seconds() // 60)}m (newest {newest}), so the prune "
            "could delete the last row holding its key",
        )
    return Plan(prune)


def run(argv):
    return subprocess.run(argv, check=True, capture_output=True, text=True).stdout


def cscli(runner, *args):
    return runner(
        [
            K3S,
            "kubectl",
            "-n",
            NAMESPACE,
            "exec",
            "deploy/crowdsec",
            "-c",
            "crowdsec",
            "--",
            "cscli",
            *args,
        ]
    )


def main(runner=run, now=None):
    now = now or datetime.now(UTC)
    rows = json.loads(cscli(runner, "bouncers", "list", "-o", "json") or "[]")
    decided = plan(rows, now)
    if decided.refused:
        print(decided.refused)
        return 1
    if not decided.prune:
        print(report(rows, rows))
        return 0
    minutes = int(WINDOW.total_seconds() // 60)
    cscli(runner, "bouncers", "prune", "-d", f"{minutes}m", "--force")
    after = json.loads(cscli(runner, "bouncers", "list", "-o", "json") or "[]")
    print(report(rows, after))
    return 0


if __name__ == "__main__":
    if any(arg in ("-h", "--help") for arg in sys.argv[1:]):
        print(__doc__.strip())
        sys.exit(0)
    try:
        sys.exit(main())
    except subprocess.CalledProcessError as exc:
        tail = (exc.stderr or exc.stdout or "").strip().splitlines()
        words = list(exc.cmd)
        start = words.index("cscli") if "cscli" in words else 1
        verb = " ".join(w for w in words[start : start + 3] if not w.startswith("-"))
        print(f"{verb} failed rc={exc.returncode}: {tail[-1] if tail else 'no output'}")
        sys.exit(1)
