"""`probe.py builder` — the host-package builder's state, its recent builds and its failures.

Sessions read `journalctl -u homelab-builder.service` by hand and counted `FAILURE` lines to
learn whether the builder was publishing (#6324). This makes one read of daniel-box and prints:

- the builder's reason and the commit it names, from the `homelab_builder_unbuilt_since`
  gauge in `homelab_builder.prom`, and how long ago its last fetch succeeded;
- every version the repository serves, from `homelab_repo.prom`;
- the newest `-n` builds, the builder's `staged <packages> at <version>` journal lines;
- the failures in the window: the builder's own lines at priority err or worse, and the
  systemd line that says a run failed. A repeated reason and tip exits 0, so systemd's
  count alone misses a builder that keeps failing quietly.

Exit 1 when the reason is anything but `ok` or `canary`, or when the last fetch is older than
two timer periods plus `TimeoutStartSec`, both read from `hosts/components/builder/units/`.
75 when the host, the textfile or the journal could not be read.
"""

# `probe_lib` is a namespace package under `scripts/`, so reaching a sibling by package name
# needs `scripts/` on sys.path — a module gets only its importer's path otherwise, and
# pyproject's `pythonpath` is a pytest setting. This has to sit ABOVE the imports below.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import re
import time

from diagnostics.probe_lib import core
from diagnostics.probe_lib.host_reader import (
    BOX,
    dry_run_text,
    journal_entries,
    read_host,
    unit_span_s,
)
from lib.deployer_park import _age_phrase
from lib.exit_codes import PROBE_FAILED, PROBE_OK
from lib.repo_paths import REPO

UNIT = "homelab-builder.service"
UNITS_DIR = REPO / "hosts" / "components" / "builder" / "units"
TEXTFILE_DIR = "/var/lib/node-exporter-textfile"
BUILDER_PROM = f"{TEXTFILE_DIR}/homelab_builder.prom"
REPO_PROM = f"{TEXTFILE_DIR}/homelab_repo.prom"
# The two reasons that publish: `canary` waits on the canary host's applied version, by design.
HEALTHY = frozenset({"ok", "canary"})

# `probe.py builder`'s help as `cli_parser._verdict_help` takes it (summary, exit 0, exit 1).
BUILDER_HELP = (
    "the host-package builder on daniel-box: its reason and commit, the last fetch, the "
    "versions the repository serves, the newest builds and the failures in the window",
    "the builder is ok or waiting on the canary, and fetched recently",
    "another reason, or the last fetch is stale",
)

_SAMPLE = re.compile(r"^(\w+)(?:\{([^}]*)\})?\s+(\S+)\s*$")
_LABEL = re.compile(r'(\w+)="([^"]*)"')
_STAGED = re.compile(r"^homelab-builder: staged (.+) at (\S+)$")


def samples(text: str) -> list[tuple[str, dict[str, str], float]]:
    """Each `name{labels} value` sample in a textfile, comments skipped."""
    out = []
    for line in text.splitlines():
        m = _SAMPLE.match(line.strip())
        if not m or line.startswith("#"):
            continue
        try:
            value = float(m.group(3))
        except ValueError:
            continue
        out.append((m.group(1), dict(_LABEL.findall(m.group(2) or "")), value))
    return out


def fetch_deadline_s(units_dir=UNITS_DIR) -> int:
    """How old the last fetch may be before it is stale: two timer periods plus a run's ceiling."""
    timer = (units_dir / "homelab-builder.timer").read_text()
    service = (units_dir / "homelab-builder.service.j2").read_text()
    return 2 * unit_span_s(timer, "OnUnitActiveSec") + unit_span_s(
        service, "TimeoutStartSec"
    )


def plan(since: str) -> dict:
    return {
        "journal": {"builder": [UNIT, since, None]},
        "files": [BUILDER_PROM, REPO_PROM],
    }


def _prom_text(doc: dict, path: str) -> str:
    read = (doc.get("files") or {}).get(path) or {}
    if read.get("status") != "ok":
        raise core.Unreadable(
            f"builder: {path} on {doc.get('host')} is {read.get('status', 'missing')}"
            f"{': ' + read['error'] if read.get('error') else ''}"
        )
    return read["text"]


def _stamp(at: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(at))


def is_failure(entry) -> bool:
    """A builder line at err or worse, or systemd's `Failed with result`, once per failed run.

    systemd's own lines sit at err too, and `Failed to start` repeats each failed run.
    """
    if entry.identifier == "systemd":
        return "Failed with result" in entry.message
    return entry.priority <= 3


def report(doc: dict, *, now: float, last: int, deadline_s: int) -> tuple[str, int]:
    """The text view and the exit code for one read of daniel-box."""
    gauges = samples(_prom_text(doc, BUILDER_PROM))
    published = [
        lab for name, lab, _ in samples(_prom_text(doc, REPO_PROM))
        if name == "homelab_repo_published_info"
    ]  # fmt: skip
    entries = journal_entries(doc, "builder")
    unbuilt = [
        (lab, v)
        for n, lab, v in gauges
        if n.endswith("_unbuilt_since_timestamp_seconds")
    ]
    fetched = [v for n, _, v in gauges if n.endswith("_fetch_timestamp_seconds")]
    if not unbuilt or not fetched:
        raise core.Unreadable(f"builder: {BUILDER_PROM} holds no reason or fetch gauge")
    labels, since = unbuilt[0]
    reason, sha = labels.get("reason", "?"), labels.get("sha", "?")
    fetch_age = int(now - fetched[0])
    problems = []
    if reason not in HEALTHY:
        problems.append(f"reason {reason}")
    if fetch_age > deadline_s:
        problems.append(f"last fetch {_age_phrase(fetch_age)} ago, past {deadline_s}s")
    behind = "caught up" if not since else f"unbuilt since {_stamp(since)}"
    lines = [
        f"builder ({doc.get('host')}): reason={reason} at {sha}, {behind}; "
        f"last fetch {_age_phrase(fetch_age)} ago"
    ]
    lines.append("published:")
    lines += [
        f"  {lab.get('package', '?'):<24}{lab.get('version', '?')}"
        for lab in sorted(published, key=lambda lab: lab.get("package", ""))
    ] or ["  none"]
    builds = [(e, _STAGED.match(e.message)) for e in entries]
    builds = [(e, m) for e, m in builds if m][-last:]
    lines.append(f"newest builds in the window ({len(builds)} shown):")
    lines += [f"  {_stamp(e.at)}  {m.group(1)} at {m.group(2)}" for e, m in builds] or [
        "  none"
    ]
    failures = [e for e in entries if is_failure(e)]
    lines.append(f"failures in the window: {len(failures)}")
    lines += [f"  {_stamp(e.at)}  {e.message}" for e in failures[-last:]]
    lines.append(f"verdict: {'; '.join(problems) or 'publishing'}")
    return "\n".join(lines), PROBE_FAILED if problems else PROBE_OK


def run_builder(ns, read=read_host, now: float | None = None) -> int:
    """Read daniel-box once, print the report, return its exit code."""
    since = f"-{core.parse_duration_seconds(ns.since)}s"
    if ns.dry_run:
        print(dry_run_text(plan(since), BOX))
        return PROBE_OK
    doc = read(plan(since), BOX)
    text, code = report(
        doc,
        now=time.time() if now is None else now,
        last=ns.last,
        deadline_s=fetch_deadline_s(),
    )
    print(text)
    return code


def add_parser(sub, verdict_help) -> None:
    """Add `builder` to probe.py's subparsers; it lives here while cli_parser.py sits at its cap."""
    bp = sub.add_parser("builder", **verdict_help(*BUILDER_HELP))
    bp.add_argument(
        "-n", dest="last", type=int, default=5, help="builds and failures to show"
    )
    bp.add_argument("--since", default="24h", help="journal window, e.g. 6h or 2d")
    bp.add_argument(
        "--dry-run", action="store_true", help="print the read without making it"
    )
