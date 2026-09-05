#!/usr/bin/env python3
"""monitor-bridge's entry point — the command line, the config load and the check loop.

The Deployment runs `python /app/cli.py` with no arguments, which loops forever at INTERVAL
seconds. Everything below the loop is a leaf: `registry.build_checks(env)` says which checks
exist, `gates.Gates` says what each reachability gate suppresses, and `check.run_once` runs one
cycle over both. Nothing here holds env-derived state after import.

Design: docs/superpowers/specs/2026-06-06-monitor-bridge-alerting-design.md
"""

import argparse
import os
import sys
import time
from collections.abc import Mapping

import bridge.common
import check
import gates
import registry
from bridge.config import load_config
from bridge.types import Check
from gates import Gates


def build_parser() -> argparse.ArgumentParser:
    """The command line. `python /app/cli.py` with no arguments is the pod's invocation."""
    parser = argparse.ArgumentParser(
        prog="cli.py",
        description=(
            "Evaluate the homelab health checks and push each result to its Uptime Kuma "
            "push monitor. With no arguments this loops forever at INTERVAL seconds, which "
            "is what the Deployment runs."
        ),
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="run one cycle and exit, instead of looping at INTERVAL seconds",
    )
    parser.add_argument(
        "--check",
        action="append",
        default=[],
        metavar="NAME",
        dest="checks",
        help=(
            "run only this check; repeatable. Validated like CHECKS_ONLY — a gate whose "
            "dependents are enabled may not be disabled — but unlike CHECKS_ONLY, the gate "
            "each named check depends on is unioned in automatically."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="evaluate every check and print the results, but push nothing to Kuma",
    )
    return parser


def main(
    argv: list[str] | None = None,
    env: Mapping[str, str] | None = None,
    checks: list[Check] | None = None,
    gate_config: Gates | None = None,
) -> int:
    """Validates the configuration and the check filter, then runs the check loop.

    Returns 2 without running any check if bridge/config.py could not parse an env value, or if
    CHECKS_ONLY/CHECKS_SKIP names an unknown check or disables a gate whose dependents are still
    enabled. `--check` is validated the same way, but first has its named checks' gates unioned
    in (see gates.expand_gates_for_cli) — CHECKS_ONLY keeps the strict env contract, `--check`
    does not need the gate spelled out alongside the check. Otherwise loops check.run_once() at
    cfg.INTERVAL seconds, touching the heartbeat file after every cycle, forever unless --once
    was given.

    Args:
      argv: The argument list, without the program name. None reads sys.argv.
      env: The environment `load_config` and `registry.build_checks` read. None reads the real
        one. See the DECIDED note below.
      checks: The registry to run. None builds it from `env`, which is what the pod does; a test
        passes the two entries it means rather than patching a module table.
      gate_config: The gate configuration `run_once` reads. None builds the production
        `Gates()`. Named `gate_config` rather than `gates` because this module imports the
        `gates` MODULE, and a parameter of that name would shadow it in the body.

    Returns:
      The process exit code: 0 after a completed --once run, 2 on a configuration fault.
    """
    args = build_parser().parse_args(argv)
    # Building the config never raises; bridge.common.CONFIG_PROBLEMS carries HTTP_TIMEOUT's own
    # parse failure, parsed there because autofix-bridge shares that module.
    #
    # DECIDED: `env` reaches `load_config` and `registry.build_checks`, and nothing else.
    # Slice 17b moved the registry out of `check.py` precisely so `main(env={...})` can decide
    # which monitor a result is pushed to, which it could not while the KUMA_PUSH_* tokens were
    # read at import into a module-level list. The FOUR reachability-gate tokens are the
    # remaining exception: `gates._gate` still reads them from `os.environ` at push time, so
    # `env` does not redirect a gate's push. They are four literals in `check.run_once` rather
    # than a table, so there is no module global left to remove — moving them would be a new
    # parameter for one caller, not the same fix.
    environment = os.environ if env is None else env
    cfg = load_config(environment, problems=bridge.common.CONFIG_PROBLEMS)
    if cfg.CONFIG_PROBLEMS:
        for problem in cfg.CONFIG_PROBLEMS:
            bridge.common.log("FATAL: bad monitor-bridge config:", problem)
        return 2
    checks = registry.build_checks(environment) if checks is None else checks
    only = (
        gates.expand_gates_for_cli(frozenset(args.checks))
        if args.checks
        else cfg.CHECKS_ONLY
    )
    problems = gates.validate_check_filter(only, cfg.CHECKS_SKIP, checks)
    if problems:
        for p in problems:
            bridge.common.log("FATAL: bad CHECKS_ONLY/CHECKS_SKIP:", p)
        return 2
    enabled = [
        c.name for c in checks if gates.check_enabled(c.name, only, cfg.CHECKS_SKIP)
    ]
    bridge.common.log(
        "monitor-bridge starting (interval=%ss, once=%s, dry_run=%s, checks=%d/%d)"
        % (cfg.INTERVAL, args.once, args.dry_run, len(enabled), len(checks))
    )
    while True:
        check.run_once(cfg, checks, dry_run=args.dry_run, only=only, gates=gate_config)
        # A --dry-run hand-run must touch nothing live, including the liveness-probe file — see
        # build_parser()'s --dry-run help.
        if not args.dry_run:
            bridge.common.touch_heartbeat(cfg.HEARTBEAT_FILE)
        if args.once:
            return 0
        time.sleep(cfg.INTERVAL)


if __name__ == "__main__":
    sys.exit(main())
