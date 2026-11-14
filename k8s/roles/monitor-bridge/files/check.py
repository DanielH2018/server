"""monitor-bridge's run loop — one full check cycle, gates first.

Stdlib only (runs on python:3.15-alpine with no extra deps). Each check returns
(ok: bool, msg: str), and the exporter records the verdict as the row's
`homelab_check_success` gauge, which the rules in observability's `bridge-exporter.yaml` page
on.

This module holds `run_once`, the gate tables it reads and the two helpers it calls. The
registry is `registry.py`, which orders the two gate rows first and gives every `Check` the
`gate` its table row names. The command line and `main()` are `cli.py` — which is what the
Deployment runs (`python /app/cli.py`). Configuration is a frozen `Config` `main()` builds once
and threads down; see this role's CLAUDE.md, *Configuration is a parameter, not a module global*.

A name the suite patches is read QUALIFIED from the module that binds it — `bridge.common.log`
— never from-imported, because a from-import copies the value in at import time and never sees
the patch. Nothing here is a module table any more: the checks arrive as the `checks` argument
and the queries through `src`, so a test states them instead of patching them. Enforced by
ansible/tests/services/test_bridge_patch_boundary.py; the census of what is patched where is
ansible/tests/services/test_monitor_bridge_modules.py.

Design: docs/monitor-bridge-internals.md.

Usage:

    python /app/cli.py [--once]

This module is a library, not the entry point. Running `check.py` directly prints a pointer
to `cli.py` on stderr and exits 2. `-h` and `--help` print this text and exit 0.
"""

import time
from collections.abc import Mapping
from types import MappingProxyType

import bridge.common
from bridge.config import Config
from bridge.sources import Sources
from bridge.types import Check, CheckFn, CheckResult, VerdictRecorder

# Checks whose `up` line is logged every cycle, because something reads it as data. The
# Longhorn Storage dashboard's four B2 panels regex the used/cap/pct/versions figures out of
# b2_storage's "B2 storage ..." message in this container's log; quiet that line and they go
# blank. ENFORCED: tests/test_check.py::test_every_dashboard_parsed_check_is_logged_when_up.
LOGGED_WHEN_UP = frozenset({"b2_storage"})

# What a dependent row logs while its gate row reads down, keyed by the gate's name. A key is
# also how `run_once` tells a gate row from any other. The dependent's body is not called and
# its gauge reads 0, and Alertmanager's inhibit rules hold the dependent's page behind the
# gate's own, so one outage pages once. The gate rows run first and have no gate of their own,
# so a gate that cannot go red is not coverage (#2825). There is no `prometheus` key: the
# Watchdog pages a dead Prometheus, which cannot evaluate a rule either.
GATE_SKIP = {
    "b2_reachable": "skipped — B2 unreachable (see B2Unreachable)",
    "wan_reachable": "skipped — WAN unreachable (see WANUnreachable)",
}

# Prometheus `job` -> the checks a dead exporter of that job skips. Scrape Targets names the dead
# exporter, so the row that reads the exporter's series pages once, not twice. host_packages
# reads the series daniel-box's node exporter publishes from its textfile directory, so a dead
# exporter makes them absent and the row would page on the absent series. M6 batch A1 moved
# disk, memory and host_temp to the `hosts` rules and batch B8 moved pi_pressure, so the map has
# no other member and no `node-pi` key. A MappingProxyType, so no test edits the one table every
# other test reads. It goes with the host_packages row.
EXPORTER_DEPENDENT: Mapping[str, frozenset[str]] = MappingProxyType(
    {"node": frozenset({"host_packages"})}
)


def check_enabled(name: str, only: frozenset[str], skip: frozenset[str]) -> bool:
    """Is `name` enabled under the CHECKS_ONLY/CHECKS_SKIP filter?

    Args:
      name: The check name to test.
      only: The enable-exactly-this-set filter. Empty enables everything.
      skip: The names to drop.
    """
    if only and name not in only:
        return False
    return name not in skip


def _evaluate(cfg: Config, src: Sources, name: str, fn: CheckFn) -> CheckResult:
    """Runs one check, converting an unreachable source/metric into a descriptive `down`.

    Keeps the loop alive instead of letting an unreachable source or metric raise and
    kill it.
    """
    try:
        return CheckResult(*fn(cfg, src))
    except Exception as e:  # an unreachable source/metric must not kill the loop
        return CheckResult(False, "%s check error: %s" % (name, e))


def down_exporters(
    up_vector: list[tuple[dict[str, str], float]],
    exporter_dependent: Mapping[str, frozenset[str]] = EXPORTER_DEPENDENT,
) -> set[str]:
    """Pure: which `exporter_dependent` jobs report up==0 in a Prometheus `up` vector.

    Fed `src.prom_vector("up")` — [(labels, value), ...]. Returns the subset of
    `exporter_dependent` keys whose Prometheus job is down.

    Args:
      up_vector: The Prometheus `up` vector as (labels, value) pairs.
      exporter_dependent: The job -> dependents map to look jobs up in.
    """
    down_jobs = {m.get("job") for m, v in up_vector if v == 0}
    return {job for job in exporter_dependent if job in down_jobs}


def _exporter_suppressed(src: Sources) -> set[str]:
    """The checks a dead exporter skips this cycle, or an empty set when the probe fails.

    A BARE `up`, deliberately — no origin_sel(). PROM_ORIGIN resolves to origin="daniel-server",
    while the `node` job labels each target with its own node name (daniel-box, daniel-server).
    A pinned probe therefore saw one node exporter of three and missed a dead daniel-box one
    (#2779). A failure to DETERMINE exporter health leaves the set empty: fail toward alerting,
    never masking.
    """
    # DECIDED: suppression stays keyed on the Prometheus `job`, so a dead exporter on ONE host
    # holds the dependents' verdicts estate-wide. Accepted over per-origin suppression: the
    # dependents report the worst origin in one verdict, so splitting suppression by origin
    # would mean splitting the checks by origin first. ScrapeTargetDown still names the dead
    # job, so the outage is reported; what is lost is a second host's verdict while one host's
    # exporter is down. HostPackageBuilderStalled in bridge-exporter.yaml carries the same
    # estate-wide exclusion in its expression.
    suppressed: set[str] = set()
    try:
        for job in down_exporters(src.prom_vector("up")):
            suppressed |= EXPORTER_DEPENDENT[job]
    except Exception as e:
        bridge.common.log("WARN: exporter-health probe failed:", e)
    return suppressed


def run_once(
    cfg: Config,
    src: Sources,
    checks: list[Check],
    only: frozenset[str] | None = None,
    export: VerdictRecorder | None = None,
) -> None:
    """Runs one full check cycle: every enabled check, the two gate rows first.

    `checks` arrives in the order `registry.build_checks` returns it, the gate rows first. A row
    whose `gate` names a gate row that read down this cycle is not run: its gauge reads 0 and
    Alertmanager's inhibit rule holds its page behind the gate's. A row under an exporter that
    is down is skipped the same way. Every other row is evaluated and logged.

    Args:
      cfg: The frozen config `main()` built — the ONLY source of configuration in a cycle.
      src: The `Sources` `main()` built — every Prometheus, Loki and HTTP query a gate or a
        check body sends goes through it. A test hands in a fake holding canned answers.
      checks: The registry to evaluate, as `registry.build_checks()` returns it. A parameter
        rather than a module table so a test hands in the entries it means. A gate row must
        come before the rows that name it; a gate the filter disables, or one absent from the
        list, reads as up.
      only: The enable-exactly-this-set filter `--check` builds. None reads cfg.CHECKS_ONLY,
        which is what the pod runs with, so it must reach EVERY check_enabled call below — a
        filter validated in main() and not passed here would print an enabled count it does
        not honour.
      export: Where each enabled row's verdict is recorded for Prometheus. A row skipped by a
        gate or an exporter records 0 and its body is not called. Any other row records
        `_evaluate`'s verdict. None records nothing, which is every caller but the looping pod.
    """
    only = cfg.CHECKS_ONLY if only is None else only
    skip = cfg.CHECKS_SKIP
    # A routine verdict is logged only on a narrowed run, where someone reads the output for
    # it. The pod logged ~50 OK lines every 300 s cycle, burying the DOWN ones; it now logs each
    # DOWN line, each `up` that is holding a down streak (a page on its way), the LOGGED_WHEN_UP
    # lines, and one CYCLE count (#4448). A SKIP line repeats what its gate's DOWN line already
    # says, so it is counted rather than logged.
    log_all = bool(only)
    counts = {"up": 0, "down": 0, "skipped": 0}
    down_gates: set[str] = set()
    # DECIDED: the exporter probe runs once per cycle, before the first row, and only when an
    # enabled row sits under `EXPORTER_DEPENDENT`. The `prometheus` gate row used to order it
    # (the probe ran after that row answered), and Cutover-2 deleted the row. A probe that
    # fails leaves the set empty, so the dependents run and fail toward alerting.
    dependents = frozenset().union(*EXPORTER_DEPENDENT.values())
    suppressed: set[str] = set()
    if any(
        entry.name in dependents and check_enabled(entry.name, only, skip)
        for entry in checks
    ):
        suppressed = _exporter_suppressed(src)

    def log_skip(name: str, msg: str) -> None:
        if log_all:
            bridge.common.log("SKIP", name, "-", msg)

    for entry in checks:
        name = entry.name
        if not check_enabled(name, only, skip):
            continue
        # The verdict the exporter records: 0 for a row a gate or an exporter skips, else the
        # body's own.
        exported = False
        # DECIDED: the gate skip stays in the bridge, ahead of the body, though Alertmanager
        # owns the paging suppression. A skipped body is never called, and that is the
        # point: a B2 cap answer is billed (`checks/b2.py:check_b2_reachable`), so calling
        # `b2_storage` during a cap breach would bill one listing every 300s cycle, and the
        # four WAN bodies would each wait out their timeouts through a WAN outage. A rule
        # cannot stop a call. Full account in
        # docs/target-architecture/monitoring/m6-retire-kuma.md, "Gates and grace".
        if entry.gate in down_gates:
            ok, msg = True, GATE_SKIP[entry.gate]
            log_skip(name, msg)
        elif name in suppressed:
            ok, msg = True, "skipped — exporter down (see ScrapeTargetDown)"
            log_skip(name, msg)
        else:
            ok, msg = _evaluate(cfg, src, name, entry.fn)
            exported = ok
            if name in GATE_SKIP and not ok:
                down_gates.add(name)
            if log_all or not ok or "streak" in msg or name in LOGGED_WHEN_UP:
                bridge.common.log("OK  " if ok else "DOWN", name, "-", msg)
        if export is not None:
            export.record(name, exported, time.time())
        if msg.startswith("skipped"):
            counts["skipped"] += 1
        else:
            counts["up" if ok else "down"] += 1
    bridge.common.log(
        "CYCLE",
        "%(up)d up, %(down)d down, %(skipped)d skipped" % counts,
        "- run with --check <name> to log every verdict",
    )


if __name__ == "__main__":
    # check.py was the entry point until this split, so `python check.py --once` is what a
    # stale runbook (or muscle memory) still reaches for. Without this it exits 0 having
    # printed nothing, which reads as a clean run rather than a no-op.
    import sys

    if any(arg in ("-h", "--help") for arg in sys.argv[1:]):
        print(__doc__.strip())
        sys.exit(0)

    print("the entry point is cli.py — run `python cli.py` instead", file=sys.stderr)
    # 64 is sysexits' EX_USAGE, the repo's usage-error code (`lib/exit_codes.py`, #4487).
    sys.exit(64)
