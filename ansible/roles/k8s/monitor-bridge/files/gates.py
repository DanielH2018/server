"""The reachability gates: which checks a single outage suppresses, and how one gate is run.

A gate differs from an ordinary check only in what its verdict is used for. `run_once` in
`check.py` evaluates the four gates first and uses each verdict to suppress that gate's
dependents, so one root cause pages once instead of storming across every monitor reading the
same source. This module owns the membership sets, the filter rules that keep a gate from being
disabled underneath its dependents, and the `Gates` seam that lets a test STATE a gate
configuration instead of patching module globals.

It is a leaf: it imports `bridge.*`, `check_table` (the `gate` column every set below derives
from) and the four gate probe bodies out of `checks.*`, and never `check`, `registry` or `cli`.

`apply_startup_grace` and `_grace_streaks` are NOT here — they live in `bridge/streaks.py`
beside the `down_streak` hysteresis they are built on, and `Gates.grace_streaks` defaults to
that module's dict. The startup grace is a gate in the same sense the other four are (it holds
a verdict back rather than reporting it), which is why the membership set `STARTUP_GRACE` is
here and the mechanism is there.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field

import bridge.common
import bridge.net
import bridge.streaks
from bridge.common import _env
from bridge.config import Config
from bridge.sources import Sources
from bridge.types import Check, CheckFn, CheckResult, push_env
from check_table import CHECKS
from checks.b2 import check_b2_reachable
from checks.cluster import check_prometheus
from checks.logs import check_loki_reachable
from checks.wan import check_wan_reachable


def _members(gate: str) -> frozenset[str]:
    """The checks whose `check_table` row names `gate` in its `gate` column."""
    return frozenset(row.name for row in CHECKS if row.gate == gate)


# Every set below is DERIVED from the `gate` column of `check_table.CHECKS`, where each member's
# row carries the reason it is a member. A row names at most one gate, which is what keeps a
# check from being suppressed by one gate and held by the startup grace at once.

# Checks that query Prometheus. A single Prometheus outage would fail every one of them at once
# — one root cause, a storm of identical pages. run_once probes Prometheus first (check_prometheus
# -> its own monitor) and, when it's unreachable, SUPPRESSES these (pushes `up` with a skip msg so
# their push-monitor heartbeat stays alive and the dead-bridge watchdog isn't tripped) so only the
# Prometheus monitor pages. A new prom_scalar/prom_vector caller takes `gate="prometheus"`.
PROM_DEPENDENT = _members("prometheus")

# One level BELOW the Prometheus gate: a single exporter down while Prometheus is UP fails every
# check reading its metrics at once. node-exporter death false-pages Root Disk + Memory (node_* go
# unavailable -> down) on top of the legitimate Scrape Targets page; cadvisor death makes
# restarts/oom/cpu read an empty vector -> silently green. Scrape Targets already names the dead
# `up{job=...}==0`, so run_once suppresses each dead exporter's dependents (pushes `up` with a skip
# msg, heartbeat kept alive) and lets Scrape Targets be the single page — the same
# one-root-cause-one-alert shape as the Prometheus gate, keyed by the Prometheus `job` label.
# Guarded by a test against the registry. (`cert`/`traefik5xx` read Traefik's own metrics, not
# these two exporters, so they're not mapped here.)
# host_temp joined 2026-08-29 with HWMON_TEMP_ORIGINS_MIN. Before the floor it had no per-host
# arm, so a single node-exporter death left it green and there was nothing to suppress. Now a
# dead node-exporter drops that host's hwmon series and trips the floor, so without this entry
# one root cause would page twice — Scrape Targets plus a coverage complaint naming the same
# host. Same reason disk and memory are here.
#
# The Pi scrapes under its OWN job — measured 2026-08-29, `count by (job, origin)
# (node_hwmon_temp_celsius)` returns job=node for daniel-server (12) and daniel-box (7) but
# job=node-pi for daniel-pi (2). This map is keyed by the Prometheus job, so a `node` entry alone
# suppresses two of the three hosts and the Pi's exporter death still double-pages. node-pi maps
# to host_temp and pi_pressure, not to disk and memory: those exclude the Pi by origin
# (HOST_METRIC_ORIGIN_EXCLUDE, since check_pi_pressure owns them), so they have nothing to
# suppress there, while the hwmon floor counts all three hosts and pi_pressure reads the Pi's
# node_* series directly (since 2026-09-18, #2004) and pages when they are absent.
EXPORTER_DEPENDENT = {
    "node": frozenset({"disk", "memory", "host_temp"}),
    "node-pi": frozenset({"host_temp", "pi_pressure"}),
}

# Loki-reachability gate — the peer of the Prometheus gate for the Loki-querying checks. A single
# Loki outage makes loki_count raise in ALL of them at once (Loki Log Ingestion + Janitorr Errors)
# -> a 2-monitor storm for one root cause. run_once probes Loki first
# (check_loki_reachable -> its own "Loki Reachable" monitor) and, when it's unreachable, SUPPRESSES
# these (pushes `up` with a skip msg so their push heartbeats stay alive) so only Loki Reachable
# pages. Loki being UP but a shipper not shipping is a different signal Loki Log Ingestion still
# surfaces (it evaluates whenever Loki is reachable).
LOKI_DEPENDENT = _members("loki_reachable")

# B2-reachability gate — the third peer of the Prometheus and Loki gates (see check_b2_reachable /
# b2_reachable in run_once), and the fix for G2/G4 of docs/archive/b2-transaction-cap-monitoring-gaps.md.
# It used to gate five kopia-era checks that read B2 health from state files written by periodic
# crons, so they reported the LAST SUCCESSFUL RUN rather than current health: on 2026-08-02 they
# read green through a nine-and-a-half-hour outage in which B2 refused every request. Those checks
# were removed 2026-08-10 — kopia is retired, backup moved to Longhorn (see
# docs/archive/k3s-migration/backup-consolidation-longhorn.md) — leaving this empty. b2_reachable itself
# stays: Longhorn still needs B2.
#
# b2_storage re-populated it on 2026-08-15. It queries B2 live rather than reading a cron's state
# file, so it does not have the stale-state fault the original five had — but it is gated for the
# other reason a gate exists: a transaction cap fails BOTH it and b2_reachable, and one root cause
# must not light two monitors.
B2_DEPENDENT = _members("b2_reachable")

# WAN-reachability gate — the fourth peer, and the one whose absence cost the most. An internet
# outage had no gate at all, so every check reaching the internet paged on its own: on
# 2026-09-18 from 05:05 a single WAN outage turned 11 tiles red inside 90 minutes (#2784).
# run_once probes two independent providers by hostname (checks/wan.py) and, when NEITHER
# answers, suppresses these so only the WAN tile pages.
#
# b2_reachable is deliberately a PEER rather than a member. It is itself a gate, and a gate
# suppressing a gate is machinery this loop does not have — GATE_DEPENDENTS' values are check
# names run_once iterates, not gates it evaluates. Two tiles for a WAN outage instead of one is
# the accepted cost; the alternative is building gate-of-a-gate for one caller.
#
# kuma_notify_failures and swallowed_verdicts also went red on 2026-09-18 and are NOT here:
# both read Loki, which is in-cluster, so the Loki gate already owns their source. What failed
# for them was Kuma's own outbound Discord send, which is a real fault the tiles should report.
WAN_DEPENDENT = _members("wan_reachable")

# Reach-out checks that poll a live app dependency (n8n/sonarr/radarr/prowlarr/scrutiny/the
# Cloudflare GraphQL API) with NO reachability gate above them and NO per-check
# hysteresis of their own — unlike
# check_ha_heartbeat/check_discord, whose HA_CONSECUTIVE/DISCORD_CONSECUTIVE grace rides out exactly
# this. On the bridge's first cycle after the weekly host reboot those dependencies are still
# starting, so an un-graced check flips its max_retries=0 monitor DOWN on that one transient cycle
# and pages (then recovers next cycle). run_once holds each of these `up` for the first
# GRACE_CYCLES-1 consecutive down cycles; the GRACE_CYCLES'th straight down still pages a
# genuinely-dead dependency. Must be DISJOINT from the run_once skip sets
# (PROM_DEPENDENT/LOKI_DEPENDENT/EXPORTER_DEPENDENT) so a graced check reaches the eval path every
# cycle. Guarded by a test against the registry: the "real check name" guard PLUS a completeness
# guard that every un-gated _get_json reach-out check is in here (prowlarr_indexers/scrutiny were
# added 2026-07-14 after they were found missing — the weekly-reboot flap's original set omitted
# them).
STARTUP_GRACE = _members("startup_grace")

# GATE_DEPENDENTS maps each reachability gate to the checks it suppresses when it is down. The
# CHECKS_ONLY/CHECKS_SKIP filter that names these lives in bridge/config.py, with the account of
# what the mechanism is for; validate_check_filter below is what refuses a filter that would
# disable a gate while leaving its dependents enabled.
GATE_DEPENDENTS = {row.name: _members(row.name) for row in CHECKS if row.is_gate}


@dataclass(frozen=True)
class Gates:
    """Everything `run_once` needs to know about suppression, as one injectable value.

    The membership sets and the four probe bodies used to be module globals `run_once` read
    directly, so a test that wanted a two-entry PROM_DEPENDENT or a stubbed Prometheus probe had
    to `monkeypatch.setattr` this module — ~25 sites in the gates suite alone, each one a
    process-wide mutation that a typo turns into a silent no-op. A test now STATES the gate
    configuration it means and hands it to `run_once`.

    Every field is read through the instance inside `run_once`; a field nothing reads would be an
    inert seam, which is why the defaults below name the module tables rather than duplicating
    them.

    Attributes:
      prom_dependent: Checks suppressed when the Prometheus gate is down.
      exporter_dependent: Prometheus `job` -> the checks a dead exporter suppresses.
        HAZARD: the default factory hands back `EXPORTER_DEPENDENT` itself, not a copy.
        `frozen=True` blocks rebinding the FIELD, never mutation THROUGH it, so
        `Gates().exporter_dependent[job] = ...` edits the module table for the whole
        process. A test that wants its own map states one; nothing may mutate this one.
      loki_dependent: Checks suppressed when the Loki gate is down.
      b2_dependent: Checks suppressed when the B2 gate is down.
      wan_dependent: Checks suppressed when neither WAN endpoint answers.
      startup_grace: Reach-out checks held `up` through their first consecutive down cycles.
      grace_streaks: The name -> consecutive-down count the startup grace mutates in place.
        Defaults to `bridge.streaks._grace_streaks`, the process-wide dict the pod uses, so a
        test that wants isolation passes its own `{}`.
        HAZARD: that default is the module's own dict, not a copy, and mutating it is the
        POINT — the hysteresis has to survive across cycles. `frozen=True` blocks rebinding
        the field, never mutation through it, so two `Gates()` built in one process share one
        streak table. Copying here would silently reset startup grace every cycle; the fix for
        cross-test bleed is an explicit `{}`, never a copying factory.
      probe_prometheus: The Prometheus gate's body.
      probe_loki: The Loki gate's body.
      probe_b2: The B2 gate's body.
      probe_wan: The WAN gate's body.
    """

    prom_dependent: frozenset[str] = PROM_DEPENDENT
    exporter_dependent: Mapping[str, frozenset[str]] = field(
        # A dict is a mutable default, which dataclasses reject outright; the factory hands back
        # the one module table rather than a copy, so the default really is the module's own map.
        default_factory=lambda: EXPORTER_DEPENDENT
    )
    loki_dependent: frozenset[str] = LOKI_DEPENDENT
    b2_dependent: frozenset[str] = B2_DEPENDENT
    wan_dependent: frozenset[str] = WAN_DEPENDENT
    startup_grace: frozenset[str] = STARTUP_GRACE
    grace_streaks: dict[str, int] = field(
        default_factory=lambda: bridge.streaks._grace_streaks
    )
    probe_prometheus: CheckFn = check_prometheus
    probe_loki: CheckFn = check_loki_reachable
    probe_b2: CheckFn = check_b2_reachable
    probe_wan: CheckFn = check_wan_reachable

    def gate_dependents(self) -> dict[str, frozenset[str]]:
        """This value's own gate -> dependents map, in GATE_DEPENDENTS' shape.

        `run_once` reads the four dependent sets through this instance, so the startup filter
        validation must read them from the same place. Reading the module table there instead
        would validate a stated `Gates` against rules the run loop does not use — the asymmetry
        would only show up as a filter accepted at startup and then behaving differently.
        """
        return {
            "prometheus": self.prom_dependent,
            "loki_reachable": self.loki_dependent,
            "b2_reachable": self.b2_dependent,
            "wan_reachable": self.wan_dependent,
        }


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


def validate_check_filter(
    only: frozenset[str],
    skip: frozenset[str],
    checks: list[Check],
    gate_dependents: Mapping[str, frozenset[str]] = GATE_DEPENDENTS,
) -> list[str]:
    """Pure: return the list of problems with a CHECKS_ONLY/CHECKS_SKIP configuration.

    Args:
      only: The enable-exactly-this-set filter. Empty enables everything.
      skip: The names to drop.
      checks: The registry the filter names checks from.
      gate_dependents: The gate -> dependents map to validate against. Defaults to the module
        table; `main` passes `Gates.gate_dependents()` so the rules validated here are the ones
        `run_once` will actually apply.
    """
    known = {c.name for c in checks} | set(gate_dependents)
    problems = ["unknown check name: %s" % n for n in sorted((only | skip) - known)]
    for gate, dependents in sorted(gate_dependents.items()):
        if check_enabled(gate, only, skip):
            continue
        enabled = sorted(d for d in dependents if check_enabled(d, only, skip))
        if enabled:
            problems.append(
                "gate %s is disabled but its dependents are enabled: %s"
                % (gate, ", ".join(enabled))
            )
    return problems


def expand_gates_for_cli(
    names: frozenset[str],
    gate_dependents: Mapping[str, frozenset[str]] = GATE_DEPENDENTS,
) -> frozenset[str]:
    """Union in every gate a `--check`-selected check depends on.

    `--check disk` alone must not trip validate_check_filter's "gate disabled but its dependents
    are enabled" refusal — the operator named the check they want, not the gate underneath it,
    and `--check` is a narrowing convenience rather than the strict CHECKS_ONLY env contract.
    CHECKS_ONLY keeps its existing all-or-nothing behaviour: an operator writing that env var is
    expected to spell out the gate themselves, same as today.

    Args:
      names: The check names passed on the command line via `--check`.
      gate_dependents: The gate -> dependents map to expand through. Defaults to the module
        table; `main` passes `Gates.gate_dependents()`, the same map it validates against.
    """
    gates = set(names)
    for gate, dependents in gate_dependents.items():
        if names & dependents:
            gates.add(gate)
    return frozenset(gates)


def down_exporters(
    up_vector: list[tuple[dict[str, str], float]],
    exporter_dependent: Mapping[str, frozenset[str]] = EXPORTER_DEPENDENT,
) -> set[str]:
    """Pure: which `exporter_dependent` jobs report up==0 in a Prometheus `up` vector.

    Fed `src.prom_vector("up")` — [(labels, value), ...]. Returns the subset of `exporter_dependent`
    keys whose Prometheus job is down, so run_once can suppress their dependents. Unit-tested.

    Args:
      up_vector: The Prometheus `up` vector as (labels, value) pairs.
      exporter_dependent: The job -> dependents map to look jobs up in. Defaults to the module
        table; `run_once` passes `gates.exporter_dependent` so the seam is not inert.
    """
    down_jobs = {m.get("job") for m, v in up_vector if v == 0}
    return {job for job in exporter_dependent if job in down_jobs}


def _evaluate(cfg: Config, src: Sources, name: str, fn: CheckFn) -> CheckResult:
    """Runs one check, converting an unreachable source/metric into a descriptive `down`.

    Keeps the loop alive instead of letting an unreachable source or metric raise and
    kill it.
    """
    try:
        return CheckResult(*fn(cfg, src))
    except Exception as e:  # an unreachable source/metric must not kill the loop
        return CheckResult(False, "%s check error: %s" % (name, e))


def _gate(
    cfg: Config,
    src: Sources,
    name: str,
    fn: CheckFn,
    dry_run: bool,
    only: frozenset[str],
) -> CheckResult:
    """Evaluate one reachability gate: verdict, log line, heartbeat push.

    A gate differs from an ordinary check only in what its verdict is used for — the check
    loop in run_once() reads it to suppress that gate's dependents, so a single outage pages
    once instead of storming. A disabled gate returns `True` so the filter suppresses nothing.

    Args:
      cfg: The gate body's config, and the skip filter. ASYMMETRIC with `only` on purpose:
        `--check` can narrow `only` per run, so run_once has a value the config does not carry;
        nothing narrows the skip set, so it is read off `cfg.CHECKS_SKIP` rather than threaded.
      src: The sources the gate body reads.
      name: The gate's own check name, as CHECKS_ONLY/CHECKS_SKIP and GATE_DEPENDENTS spell it.
      fn: The gate's check body, taken off the `Gates` value run_once was given.
      dry_run: Evaluate and log, but push nothing to Kuma.
      only: The enable-exactly-this-set filter.
    """
    if not check_enabled(name, only, cfg.CHECKS_SKIP):
        return CheckResult(True, "disabled by check filter")
    ok, msg = _evaluate(cfg, src, name, fn)
    bridge.common.log("OK  " if ok else "DOWN", name, "-", msg)
    if not dry_run:
        bridge.net.push(cfg, _env(push_env(name), ""), ok, msg)
    return CheckResult(ok, msg)
