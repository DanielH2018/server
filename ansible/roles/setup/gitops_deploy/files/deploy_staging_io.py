#!/usr/bin/env python3
"""The staging gate's I/O shell: ask daniel-stage about a commit, and record what it said.

`deploy_staging.py` holds the verdict logic and has to stay import-pure — `deploy_logic.py`
re-exports it to three tools in `scripts/deploy_tools/` that import with only this directory on
`sys.path`, so an `import deploy_io` there reaches `host_lib` and breaks `land.sh`. This module
is the half that runs the scripts, writes the ledger and posts the alert.

It lived at the bottom of `deploy_handlers.py` until 2026-09-11, where `handle_k8s` is its only
production caller. What moved it out was that file reaching its length cap: a 100-line I/O shell
for one subsystem is the piece a handlers module is least about.

Reach `deploy_io` and `deploy_alerts` qualified, never by from-import.
"""

import deploy_alerts
import deploy_io
from deploy_config import CHICAGO, Config, log
from deploy_staging import (
    STAGING_SKIPPED,
    staging_scope,
    staging_tick_outcome,
    staging_verdict,
    staging_verdict_summary,
)
from deploy_state import DeployerState
from deploy_toolbox import DeployTools


def record_staging_tick(
    tools: DeployTools,
    state: DeployerState,
    sha: str,
    gated: set[str],
    verdict: str,
) -> None:
    """Append this tick's verdict to the tick ledger. Never raises. See deploy_io.

    A tick that measured nothing writes nothing — `staging_tick_outcome` returns None for
    SKIPPED, and the tick runs every ten minutes, so recording those would bury the real
    samples. That decision is made HERE rather than inside `deploy_io.record_staging_tick`,
    which would otherwise have to import this module and close a cycle through
    `deploy_toolbox`.
    """
    outcome = staging_tick_outcome(verdict)
    if outcome is None:
        return
    deploy_io.record_staging_tick(
        state.path("staging_ticks"),
        CHICAGO,
        tools.now,
        sha,
        gated,
        verdict,
        outcome,
    )


def consume_staging_override(state: DeployerState) -> bool:
    """Spend the operator's one-tick override, if it is armed. True when it was."""
    return deploy_io.consume_override(state.path("staging_override"))


def consult_staging(
    tools: DeployTools,
    state: DeployerState,
    config: Config,
    services: set[str],
    origin: str,
) -> str:
    """Ask the staging cluster about this commit, and return the one-word verdict.

    The verdict is `staging_verdict`'s vocabulary: pass, rejected, no_verdict, or skipped when
    nothing was asked at all. Whether it stops the prod deploy is `staging_blocks`' decision, not
    this function's — returning a word and acting on it are kept apart so the gate can stay
    advisory (slice 3) while the verdict is already the thing being logged and measured.

    NOTHING HERE MAY BREAK A PROD DEPLOY, blocking or not. Every failure path — a missing script,
    an ssh outage, a wedged guest, a bug in this function — is caught by
    `deploy_io.run_staging_scripts` and reported as NO VERDICT, which `staging_blocks` never
    blocks on. An internal error alerts on the same path as any other non-PASS: a silent
    pass-through would make a bug here the one way past the gate that nobody sees.

    Off by default (`STAGING_GATE` in the unit's env). Turning it on costs every k8s tick the
    staging deploy's wall-clock, which is why it is a switch rather than a given.
    """
    if not config.staging_gate:
        return STAGING_SKIPPED
    # An ARMED gate with an empty subset can never gate anything, and the SKIPPED it returns
    # below is the same word a tick that simply touched no staging service gets. Those two
    # states are worth telling apart in the journal: the second is the ordinary case, the first
    # means the operator turned the gate on and it is doing nothing. `load_config` does not
    # parse STAGING_SUBSET — it is a `gitops_deploy.py` constant that `tick_config()` snapshots
    # — so a Config built anywhere else carries the fail-safe empty default and lands here.
    if not config.staging_subset:
        log(
            "staging: gate is ARMED but STAGING_SUBSET is empty — nothing can be gated, so "
            "every service is reported unchecked"
        )
    gated, ungated = staging_scope(services, config.staging_subset)
    if not gated:
        log(staging_verdict_summary(gated, ungated, 0, 0))
        return STAGING_SKIPPED

    deploy_rc, expect_rc = tools.run_staging_scripts(
        config.repo,
        origin,
        ",".join(sorted(gated)),
        config.staging_gate_timeout_s,
        config.staging_expect_timeout_s,
    )
    summary = staging_verdict_summary(gated, ungated, deploy_rc, expect_rc)
    log(summary)
    # Alerted, not silent: a journal line alone collects no operator judgement about whether a
    # failure was staging's fault or the change's, which is the one thing the entry condition's
    # false-failure rate is made of.
    if deploy_rc != 0 or expect_rc != 0:
        deploy_alerts.alert_once(
            tools,
            state,
            config,
            "staging_alerted",
            "staging",
            origin,
            deploy_alerts.staging_verdict_alert(
                origin, summary, config.staging_gate_blocking
            ),
        )
    verdict = staging_verdict(deploy_rc, expect_rc)
    record_staging_tick(tools, state, origin, gated, verdict)
    return verdict
