"""monitor-bridge's run loop — one full check cycle, gates first.

Stdlib only (runs on python:3.14-alpine with no extra deps). Each check returns
(ok: bool, msg: str) and maps to one Kuma *push* monitor. Every loop iteration pushes
the result (status=up|down): an explicit `down` gives fast, descriptive alerts, while
the Kuma push monitor's heartbeat interval is the backstop for "the bridge itself died"
(all pushes stop).

This module holds `run_once` and nothing else. The registry is `registry.py`, the gate sets and
the `Gates` seam are `gates.py`, the command line and `main()` are `cli.py` — which is what the
Deployment runs (`python /app/cli.py`). Configuration is a frozen `Config` `main()` builds once
and threads down; see this role's CLAUDE.md, *Configuration is a parameter, not a module global*.

A name the suite patches is read QUALIFIED from the module that binds it — `bridge.net.push`,
`bridge.common.log` — never from-imported, because a from-import copies the value in at import
time and never sees the patch. Nothing here is a module table any more: the checks arrive as the
`checks` argument and every gate fact through `gates`, so a test states them instead of patching
them. Enforced by ansible/tests/services/test_bridge_patch_boundary.py; the census of what is
patched where is ansible/tests/services/test_monitor_bridge_modules.py.

Design: docs/monitor-bridge-internals.md.
"""

import bridge.common
import bridge.net
import bridge.streaks

# Aliased because `gates` is also the name of run_once's parameter — the Gates VALUE a caller
# passes. `gate_lib` is the module the gate sets and helpers are defined in; a bare `import
# gates` would make the parameter shadow it inside the function body.
import gates as gate_lib
from bridge.config import Config
from bridge.types import Check
from gates import Gates


def run_once(
    cfg: Config,
    checks: list[Check],
    gates: Gates,
    dry_run: bool = False,
    only: frozenset[str] | None = None,
) -> None:
    """Runs one full check cycle: the reachability gates, then every enabled check.

    Evaluates the Prometheus, Loki, B2 and WAN gates first, so a single
    outage in one of them suppresses its dependent checks (pushed `up` with a skip
    message) instead of paging each of them separately. Every enabled check in `checks` is
    then evaluated (unless suppressed by a gate or an exporter outage) and its result is
    logged and pushed to its Kuma monitor.

    Args:
      cfg: The frozen config `main()` built — the ONLY source of configuration in a cycle.
      checks: The registry to evaluate, as `registry.build_checks(env)` returns it. A
        parameter rather than a module table so a test hands in the two entries it means.
      dry_run: Evaluate and log every check, but push nothing to Kuma. Defaults to False, so
        the pod's own `python /app/cli.py` and every existing caller are unchanged.
      only: The enable-exactly-this-set filter `--check` builds. None reads cfg.CHECKS_ONLY,
        which is what the pod runs with, so it must be threaded to EVERY check_enabled call
        below — a filter validated in main() and not passed here would print an enabled count
        it does not honour.
      gates: Which checks each gate suppresses, and the four gate bodies. REQUIRED: cli.main()
        builds the one production `Gates()` per process and passes it every cycle, so
        `Gates.grace_streaks` binds `bridge.streaks._grace_streaks` once at start; the dict is
        only ever mutated, never rebound, so the pin is safe. A default here would let a caller
        that forgot the argument build a SECOND production Gates with its own streak dict,
        silently resetting startup-grace hysteresis every cycle.
    """
    only = cfg.CHECKS_ONLY if only is None else only
    skip = cfg.CHECKS_SKIP
    # Prometheus reachability is evaluated FIRST and gates the prom-dependent checks: a single
    # Prometheus outage would otherwise page all of them at once (one root cause, an alert storm).
    # When it's down they're suppressed (pushed `up` with a skip msg, keeping each push monitor's
    # heartbeat alive) so only the Prometheus monitor pages; a real per-metric problem still alerts
    # whenever Prometheus is up.
    prom_ok, _prom_msg = gate_lib._gate(
        cfg, "prometheus", gates.probe_prometheus, dry_run, only
    )

    # Exporter-reachability gate (one level below the Prometheus gate): when Prometheus is up, probe
    # `up` once and suppress each dead exporter's dependents so a node-exporter/cadvisor death is one
    # page (Scrape Targets), not a 3-monitor false-page storm / silent-green split. A failure to
    # DETERMINE exporter health leaves `suppressed` empty (fail toward alerting, never masking).
    #
    # A BARE `up`, deliberately — no origin_sel(). PROM_ORIGIN resolves to origin="daniel-server",
    # while the `node` job labels each target with its own node name (daniel-box, daniel-server)
    # and `node-pi` is statically labelled daniel-pi. A pinned probe therefore saw one node
    # exporter of three, so EXPORTER_DEPENDENT["node-pi"] was unreachable and the daniel-box half
    # of "node" with it: on 2026-09-21 15:09 pi_pressure paged with "node-pi series missing" —
    # exactly what that entry exists to suppress — and on 2026-09-26 one Pi outage paged both
    # pi_pressure and cluster_targets (#2779).
    #
    # DECIDED: suppression stays keyed on the Prometheus `job`, so a dead exporter on ONE host
    # now suppresses its dependents estate-wide — daniel-box's node exporter dying holds
    # daniel-server's disk and memory verdicts too. Accepted over per-origin suppression: the
    # dependents report the worst origin in one verdict, so splitting suppression by origin would
    # mean splitting the checks by origin first. Scrape Targets still names the dead job, so the
    # outage is reported; what is lost is a second host's verdict while one host's exporter is
    # down. check_targets_down keeps its origin_sel() pin — TARGETS_MIN is sized to it.
    suppressed = set()
    if prom_ok and gate_lib.check_enabled("prometheus", only, skip):
        try:
            for job in gate_lib.down_exporters(
                bridge.net.prom_vector(cfg, "up"),
                gates.exporter_dependent,
            ):
                suppressed |= gates.exporter_dependent[job]
        except Exception as e:
            bridge.common.log("WARN: exporter-health probe failed:", e)

    # Loki-reachability gate (peer of the Prometheus gate): probe Loki once so a single Loki outage
    # is one page (Loki Reachable), not a storm across every Loki-querying check (loki_dependent).
    loki_ok, _loki_msg = gate_lib._gate(
        cfg,
        "loki_reachable",
        gates.probe_loki,
        dry_run,
        only,
    )

    # B2-reachability gate (peer of the two above): B2 caps TRANSACTIONS separately from storage
    # bytes, and the kopia-era state-file checks this used to gate all reported their last
    # successful cron run rather than current B2 health — the 2026-08-02 transaction-cap incident.
    # Those checks are gone (backup moved to Longhorn), but b2_reachable stays: Longhorn still
    # needs B2. The probe is throttled inside b2_reachable (it must not spend the transaction
    # budget it is watching), but the cached verdict is pushed every cycle so this monitor's own
    # heartbeat stays alive.
    b2_ok, _b2_msg = gate_lib._gate(cfg, "b2_reachable", gates.probe_b2, dry_run, only)

    # WAN-reachability gate (peer of the two above): an internet outage had no gate at all,
    # so every check reaching the internet paged on its own — 11 tiles red inside 90 minutes on
    # 2026-09-18 (#2784). Two independent providers probed by hostname, down only when NEITHER
    # answers, so a single provider's outage does not silence a dependent reading the other.
    wan_ok, _wan_msg = gate_lib._gate(
        cfg, "wan_reachable", gates.probe_wan, dry_run, only
    )

    for entry in checks:
        name, token, fn = entry.name, entry.token, entry.fn
        if not gate_lib.check_enabled(name, only, skip):
            continue
        if not prom_ok and name in gates.prom_dependent:
            ok, msg = True, "skipped — Prometheus unreachable (see Prometheus monitor)"
            bridge.common.log("SKIP", name, "-", msg)
        elif not loki_ok and name in gates.loki_dependent:
            ok, msg = True, "skipped — Loki unreachable (see Loki Reachable monitor)"
            bridge.common.log("SKIP", name, "-", msg)
        elif not b2_ok and name in gates.b2_dependent:
            ok, msg = True, "skipped — B2 unreachable (see B2 Reachable monitor)"
            bridge.common.log("SKIP", name, "-", msg)
        elif not wan_ok and name in gates.wan_dependent:
            ok, msg = True, "skipped — WAN unreachable (see WAN Reachable monitor)"
            bridge.common.log("SKIP", name, "-", msg)
        elif name in suppressed:
            ok, msg = True, "skipped — exporter down (see Scrape Targets)"
            bridge.common.log("SKIP", name, "-", msg)
        else:
            ok, msg = gate_lib._evaluate(cfg, name, fn)
            if name in gates.startup_grace:
                ok, msg = bridge.streaks.apply_startup_grace(
                    name, ok, msg, cfg.GRACE_CYCLES, gates.grace_streaks
                )
            bridge.common.log("OK  " if ok else "DOWN", name, "-", msg)
        if not dry_run:
            bridge.net.push(cfg, token, ok, msg)


if __name__ == "__main__":
    # check.py was the entry point until this split, so `python check.py --once --dry-run` is
    # what a stale runbook (or muscle memory) still reaches for. Without this it exits 0 having
    # printed nothing and pushed nothing, which reads as a clean dry run rather than a no-op.
    import sys

    print("the entry point is cli.py — run `python cli.py` instead", file=sys.stderr)
    sys.exit(2)
