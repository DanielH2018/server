"""The cross-file timeout sums nothing else pins.

"The rollback survives max flock contention" is split across config.env.j2 (the run and
health budgets), defaults/main.yml (the k8s deploy, rollback and staging budgets) and
gitops-deploy.service.j2 (the flock wait and TimeoutStartSec). The weekly secret-rotate cron
and deploy.sh wait on the same lock, so their waits must clear the deployer's worst hold, and
K8S_ROLLBACK_TIMEOUT_S must cover one full revert cycle for the worst promoted service. Every
value is read from its source rather than pinned, so a bump to any one of them fails here
instead of silently reopening the gap.
"""

# ansible/roles/setup/gitops_deploy/files/test_gitops_deploy_timeout_budgets.py

import pathlib
import re

import yaml

# "The rollback survives max flock contention" is an invariant split across two templates:
#   config.env.j2            -> RUN_BUDGET_S (health-gate budget) + HEALTH_TIMEOUT_S (rollback redeploy)
#   gitops-deploy.service.j2 -> flock -w <N> (max lock wait) + TimeoutStartSec (systemd hard kill)
# RUN_START is measured AFTER flock acquires, but TimeoutStartSec counts from unit activation and so
# INCLUDES the flock wait — so the worst case flock_wait + RUN_BUDGET_S + HEALTH_TIMEOUT_S must fit
# inside TimeoutStartSec, else systemd SIGTERMs the deployer mid-rollback and the bad commit is
# stranded live (the failure 1ba4fbb2 sized these four values to avoid, down to zero slack). Nothing
# else pins the cross-file sum, so a later bump to any one value would silently reopen it while every
# other test stays green — the same class the write_hold / divergence-marker guards above pin.

_TEMPLATES = pathlib.Path(__file__).parents[1] / "templates"


def _search1(pattern: str, text: str) -> str:
    m = re.search(pattern, text, re.MULTILINE)
    assert m is not None, f"pattern {pattern!r} did not match — template renamed?"
    return m.group(1)


def _systemd_seconds(span: str) -> int:
    # Parse the systemd time spans this unit actually uses (Nmin / Ns / bare seconds).
    m = re.fullmatch(r"(\d+)\s*(min|m|sec|s|)", span.strip())
    assert m is not None, f"unrecognized systemd time span {span!r}"
    return int(m.group(1)) * (60 if m.group(2) in ("min", "m") else 1)


def test_deploy_timeout_budget_survives_max_flock_contention():
    env = (_TEMPLATES / "config.env.j2").read_text()
    unit = (_TEMPLATES / "gitops-deploy.service.j2").read_text()
    flock_wait = int(_search1(r"^ExecStart=.*?flock\s+-w\s+(\d+)", unit))
    run_budget = int(_search1(r"^RUN_BUDGET_S=(\d+)", env))
    health_timeout = int(_search1(r"^HEALTH_TIMEOUT_S=(\d+)", env))
    timeout_start = _systemd_seconds(_search1(r"^TimeoutStartSec=(\S+)", unit))
    budget = flock_wait + run_budget + health_timeout
    assert budget <= timeout_start, (
        f"flock -w {flock_wait} + RUN_BUDGET_S {run_budget} + HEALTH_TIMEOUT_S {health_timeout} "
        f"= {budget}s must fit inside TimeoutStartSec {timeout_start}s, or a slow health-gate under "
        f"max flock contention gets SIGTERMed mid-rollback and the bad commit is stranded live "
        f"(see 1ba4fbb2)."
    )


# A second, independent invariant from the Docker one above: on the k8s path, a failed forward
# deploy and its rollback redeploy run SEQUENTIALLY inside one systemd unit activation, each
# bounded by its own K8S_DEPLOY_TIMEOUT_S / K8S_ROLLBACK_TIMEOUT_S rather than by RUN_BUDGET_S.
# Both values are Jinja references in config.env.j2, not literals, so this reads their source —
# defaults/main.yml — instead of the rendered template.

_DEFAULTS = pathlib.Path(__file__).parents[1] / "defaults" / "main.yml"


def _worst_lock_hold(defaults: dict) -> int:
    """Longest one gitops-deploy activation can hold the git-tree lock, EXCLUDING its own flock
    wait (which is spent before the lock is held).

    All four terms are on the SAME path and are additive, not alternative: consult_staging runs
    inside `if cs.k8s_deploy:` in main(), ahead of deploy_k8s, so an activation that stalls the
    staging gate and then stalls both playbook budgets spends all four in sequence. The BROAD arm
    returns before that block and so cannot stack with any of them.

    The staging terms are counted even though gitops_deploy_staging_gate is false by default. The
    host that has the gate ON is the one whose budget has to fit, and a budget that only holds
    while a feature is off is not a budget — that reading is exactly how the 2026-08-29 review's
    H-1 got in: 1200 + 180 sat unsummed inside a 2700s ceiling and every check read green.
    """
    return (
        int(defaults["gitops_deploy_staging_gate_timeout_s"])
        + int(defaults["gitops_deploy_staging_expect_timeout_s"])
        + int(defaults["gitops_deploy_k8s_timeout_s"])
        + int(defaults["gitops_deploy_k8s_rollback_timeout_s"])
    )


def _budget_fits(defaults: dict, flock_wait: int, timeout_start: int) -> bool:
    return flock_wait + _worst_lock_hold(defaults) <= timeout_start


def test_k8s_deploy_timeout_budget_survives_max_flock_contention():
    unit = (_TEMPLATES / "gitops-deploy.service.j2").read_text()
    defaults = yaml.safe_load(_DEFAULTS.read_text())
    flock_wait = int(_search1(r"^ExecStart=.*?flock\s+-w\s+(\d+)", unit))
    timeout_start = _systemd_seconds(_search1(r"^TimeoutStartSec=(\S+)", unit))
    hold = _worst_lock_hold(defaults)
    assert _budget_fits(defaults, flock_wait, timeout_start), (
        f"flock -w {flock_wait} + the worst-case lock hold {hold}s (staging gate + staging "
        f"expectations + K8S_DEPLOY_TIMEOUT_S + K8S_ROLLBACK_TIMEOUT_S) = "
        f"{flock_wait + hold}s must fit inside TimeoutStartSec {timeout_start}s, or a stalled "
        f"forward deploy followed by a stalled rollback gets SIGTERMed mid-rollback, stranding "
        f"the bad commit live with the volume revert possibly half-done (task 6b)."
    )


def test_an_uncounted_staging_budget_is_caught():
    # Red proof for the three budget tests that share _worst_lock_hold. They can only ever be
    # observed passing, so this drives the same verdict function with the pre-fix numbers: the
    # 2026-08-29 review's H-1, where a 1200s gate and a 180s expectation check sat inside a
    # 2700s ceiling that nothing summed them into.
    sized = {
        "gitops_deploy_staging_gate_timeout_s": 600,
        "gitops_deploy_staging_expect_timeout_s": 120,
        "gitops_deploy_k8s_timeout_s": 900,
        "gitops_deploy_k8s_rollback_timeout_s": 1320,
    }
    assert _worst_lock_hold(sized) == 2940
    assert _budget_fits(sized, 180, 3600)

    h1 = {
        **sized,
        "gitops_deploy_staging_gate_timeout_s": 1200,
        "gitops_deploy_staging_expect_timeout_s": 180,
    }
    assert _worst_lock_hold(h1) == 3600
    assert not _budget_fits(h1, 180, 2700), (
        "the budget check must REJECT the H-1 shape (180 + 1200 + 180 + 900 + 1320 = 3780s "
        "against TimeoutStartSec 2700s); a check that passes it is measuring nothing."
    )


_SECRET_ROTATE = (
    pathlib.Path(__file__).parents[2]
    / "initial_setup"
    / "templates"
    / "secret-rotate.sh.j2"
)


def test_secret_rotate_lock_wait_clears_the_deployers_worst_case_hold():
    # 2026-08-22 review M4. gitops-deploy.service wraps its whole ExecStart in
    # /var/lock/server-git-tree.lock, and one activation can run the forward deploy budget and
    # then, in the failure path, the rollback budget — sequentially, inside that one hold. The
    # weekly secret-rotate cron waits on the same lock.
    #
    # At `flock -w 1200` against a 2220s worst case the cron gave up mid-incident and SKIPPED
    # that week's rotation. crons.yml installs one weekly entry with no retry, and
    # ROTATE_LEAD_DAYS=8 against a 7-day cadence means a token usually gets exactly one eligible
    # run — so a skipped week can put a token overdue.
    #
    # Derived from the same sources the two budgets above read, so bumping either deploy timeout
    # fails this test instead of silently re-opening the gap. A single failing service reaches
    # the worst case; it does not need a batch.
    defaults = yaml.safe_load(_DEFAULTS.read_text())
    worst_hold = _worst_lock_hold(defaults)

    cron_wait = int(_search1(r"^flock\s+-w\s+(\d+)\s+9", _SECRET_ROTATE.read_text()))
    assert cron_wait >= worst_hold, (
        f"secret-rotate's `flock -w {cron_wait}` must clear gitops-deploy's worst-case lock hold "
        f"({worst_hold}s: the staging gate and expectation check, then K8S_DEPLOY_TIMEOUT_S and "
        f"K8S_ROLLBACK_TIMEOUT_S), or a legitimate long rollback makes the weekly rotation skip a week "
        f"with no retry (2026-08-22 review M4)."
    )


# K8S_ROLLBACK_TIMEOUT_S must cover one full rollback cycle for the most expensive currently-
# promoted (k8s_autodeploy: true) service that also declares k8s_autodeploy_snapshot_pvcs: the
# pre-revert snapshot wait, the revert itself, the forward apply's own rollout wait, and the
# post-rollout stabilisation soak — all inside the SAME playbook run, on one continuous timeline
# where nothing fails (a failure aborts the whole play immediately, so it can never compound with
# an independent failure elsewhere — see gitops_deploy/CLAUDE.md's rollback-timeout section).
#
# Deliberately a PER-SERVICE bound, not a per-batch one: co-batched claim-declaring services
# stack their snapshot+revert phases (only the rollout WAIT is deduped across a batch, via
# roles/k8s/rollout-drain), so a multi-service batch is NOT covered here — that gap is recorded
# in gitops_deploy/CLAUDE.md ("the batch-abort blast radius") and in this same defaults/main.yml
# comment, deliberately not modeled by this test.
#
# Computed from role SOURCES, not pinned numbers, so a future rollout-timeout bump or a new
# promoted claim-declaring role fails this test instead of silently under-sizing the budget.

_K8S_ROLES_DIR = pathlib.Path(__file__).parents[3] / "k8s"
_ALL_VARS = pathlib.Path(__file__).parents[4] / "inventory" / "group_vars" / "all.yml"
_MANIFESTS_ROLLOUT_DEFAULT_S = (
    300  # k8s/manifests default: manifests_rollout_timeout | default('300s')
)


def _rollout_timeout_s(role: str) -> int:
    tasks_path = _K8S_ROLES_DIR / role / "tasks" / "main.yml"
    text = tasks_path.read_text() if tasks_path.exists() else ""
    m = re.search(r"manifests_rollout_timeout:\s*(\d+)s", text)
    return int(m.group(1)) if m else _MANIFESTS_ROLLOUT_DEFAULT_S


def test_k8s_rollback_budget_covers_the_worst_single_promoted_service():
    revert_defaults = yaml.safe_load(
        (_K8S_ROLES_DIR / "volume-revert" / "defaults" / "main.yml").read_text()
    )
    snapshot_defaults = yaml.safe_load(
        (_K8S_ROLES_DIR / "volume-snapshot" / "defaults" / "main.yml").read_text()
    )
    all_vars = yaml.safe_load(_ALL_VARS.read_text())
    defaults = yaml.safe_load(_DEFAULTS.read_text())

    state_timeout = int(revert_defaults["volume_revert_state_timeout"])
    api_timeout = int(revert_defaults["volume_revert_api_timeout"])
    snapshot_timeout = int(snapshot_defaults["volume_snapshot_timeout"])
    stabilise = int(all_vars["k8s_rollout_stabilise_seconds"])
    rollback_timeout = int(defaults["gitops_deploy_k8s_rollback_timeout_s"])
    per_claim = snapshot_timeout + 3 * state_timeout + 3 * api_timeout

    worst_role, worst_ceiling, worst_claims = None, 0, 0
    for role_defaults_path in sorted(_K8S_ROLES_DIR.glob("*/defaults/main.yml")):
        role = role_defaults_path.parent.parent.name
        role_defaults = yaml.safe_load(role_defaults_path.read_text()) or {}
        if not role_defaults.get("k8s_autodeploy"):
            continue
        claims = role_defaults.get("k8s_autodeploy_snapshot_pvcs") or []
        if not claims:
            continue
        ceiling = len(claims) * per_claim + _rollout_timeout_s(role) + stabilise
        if ceiling > worst_ceiling:
            worst_role, worst_ceiling, worst_claims = role, ceiling, len(claims)

    assert worst_role is not None, (
        "no promoted (k8s_autodeploy: true), claim-declaring k8s role found — the sizing model "
        "this test encodes no longer matches the repo; update it rather than deleting it"
    )
    assert worst_ceiling <= rollback_timeout, (
        f"{worst_role} needs {worst_ceiling}s for one full rollback cycle "
        f"({worst_claims} claim(s), "
        f"{_rollout_timeout_s(worst_role)}s rollout), which exceeds "
        f"gitops_deploy_k8s_rollback_timeout_s ({rollback_timeout}s) — its rollback can be "
        f"SIGTERMed mid-revert. Raise that default (and TimeoutStartSec, and re-check this "
        f"test's own comment on the batch-summation gap it does not cover)."
    )


_DEPLOY_SH = pathlib.Path(__file__).parents[5] / "scripts" / "deploy.sh"


def test_deploy_sh_lock_wait_clears_the_deployers_worst_case_hold():
    # 2026-08-23b review M13. The sibling above pins the weekly secret-rotate cron's wait
    # against the same worst case. deploy.sh computes the identical quantity by hand, and its
    # own comment records that the hand-derived value already rotted once: 1500 stayed put
    # through two TimeoutStartSec bumps. Deriving it from the same defaults the deployer reads
    # means the next bump fails here instead of silently shortening an operator's wait.
    defaults = yaml.safe_load(_DEFAULTS.read_text())
    worst_hold = _worst_lock_hold(defaults)

    lock_wait = int(_search1(r"^LOCK_WAIT=(\d+)", _DEPLOY_SH.read_text()))
    assert lock_wait >= worst_hold, (
        f"deploy.sh's LOCK_WAIT={lock_wait} must clear gitops-deploy's worst-case lock hold "
        f"({worst_hold}s: the staging gate and expectation check, then K8S_DEPLOY_TIMEOUT_S and "
        f"K8S_ROLLBACK_TIMEOUT_S), or an operator deploy queued behind a legitimately long rollback exits "
        f"75 having deployed nothing (2026-08-23b review M13)."
    )
