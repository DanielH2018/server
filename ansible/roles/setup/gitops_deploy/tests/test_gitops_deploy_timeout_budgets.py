"""The cross-file timeout sums nothing else pins.

"The rollback survives max flock contention" is split across config.env.j2 (the run and
health budgets), defaults/main.yml (the k8s deploy, rollback and staging budgets) and
gitops-deploy.service.j2 (the flock wait and TimeoutStartSec). Four other jobs wait on the same
lock, so every one of their waits must clear the deployer's worst hold, and
K8S_ROLLBACK_TIMEOUT_S must cover one full revert cycle for the worst promoted service. Every
value is read from its source rather than pinned, so a bump to any one of them fails here
instead of silently reopening the gap.

The forward cap is derived the same way as the rollback budget: from the worst promoted role's
own waits, plus a lock-wait allowance and playbook overhead (#2397).

The lock waiters are checked as a CENSUS (`_LOCK_WAITERS`) rather than one test each. Two of
the four were pinned individually and the other two were not, so docs-refresh and eval-run sat
at 2700 against a 2940s hold with every check green — and docs-refresh's comment claimed it
matched secret-rotate, which was 3000. A per-consumer test only ever covers the consumers
somebody remembered to write one for.
"""

# ansible/roles/setup/gitops_deploy/tests/test_gitops_deploy_timeout_budgets.py

import pathlib
import re

import pytest
import yaml

from _helpers import manifests_rollout_timeout_s
from _role_tasks import in_role_wait_s

# "The rollback survives max flock contention" is an invariant split across two templates:
#   config.env.j2            -> RUN_BUDGET_S (health-gate budget) + HEALTH_TIMEOUT_S (rollback redeploy)
#   gitops-deploy.service.j2 -> flock -w <N> (max lock wait) + TimeoutStartSec (systemd hard kill)
# `DeployTools.run_start` (built in entrypoint(), so AFTER flock acquires) is what the gate measures
# its deadline from, but TimeoutStartSec counts from unit activation and so
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
    """The four k8s-path terms of the git-tree lock hold, EXCLUDING this unit's own flock wait
    (which is spent before the lock is held).

    NOT the longest hold on every path, and the difference is named rather than papered over:
    the DOCKER `deploy_io.deploy()` runs `ansible-playbook` with no timeout at all, and waits
    up to `deploy_locks.SERVICE_LOCK_WAIT_S` for its service locks, both inside this same hold.
    Nothing bounds that path, so nothing here can sum it. It is unreachable on the only host
    that runs this unit — daniel-box declares every containers_list entry `platform: k8s` — and
    the day a Docker service lands there, this function is what has to grow a fifth term.

    All four terms are on the SAME path and are additive, not alternative: consult_staging runs
    inside `if cs.k8s_deploy:` in main(), ahead of deploy_k8s, so an activation that stalls the
    staging gate and then stalls both playbook budgets spends all four in sequence. The BROAD arm
    returns before that block and so cannot stack with any of them.

    Each term bounds its phase WHOLE: since ADR-0017 a k8s phase also waits for one service lock
    per tag, and it waits while this unit holds the git-tree lock. `deploy_locks.locked_budget`
    is what keeps the term true — it shares one deadline between that wait and the playbook, so
    a phase queued behind an operator's deploy still cannot hold the lock for longer than its
    own timeout. Give the wait a budget of its own and every k8s term here doubles while this
    file keeps reading green;
    `ansible/tests/deploy/test_deploy_runs_from_a_snapshot_under_service_locks.py::test_a_budgeted_deploy_shares_one_deadline_between_its_wait_and_its_run`
    is the guard that fails instead.

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


_INITIAL_SETUP_TEMPLATES = (
    pathlib.Path(__file__).parents[2] / "initial_setup" / "templates"
)
_SECRET_ROTATE = _INITIAL_SETUP_TEMPLATES / "secret-rotate.sh.j2"
_REPO = pathlib.Path(__file__).parents[5]

# Every job that waits for /var/lock/server-git-tree.lock, with the regex that reads its wait.
#
# WHY A CENSUS AND NOT ONE TEST EACH. Two of these four were pinned individually (secret-rotate
# from the 2026-08-22 review M4, deploy.sh from 2026-08-23b M13) and the other two were not, so
# docs-refresh and eval-run sat at 2700 against a 2940s hold while every check read green.
# docs-refresh's own comment claimed it "matches secret-rotate" while secret-rotate was 3000.
# A per-consumer test only covers the consumers somebody remembered to write one for; this
# table is the thing a new waiter has to be added to, and the non-vacuity test below is what
# makes forgetting fail rather than pass silently.
_LOCK_WAITERS = {
    "secret-rotate.sh.j2": (_SECRET_ROTATE, r"^flock\s+-w\s+(\d+)\s+9"),
    "docs-refresh.sh.j2": (
        _INITIAL_SETUP_TEMPLATES / "docs-refresh.sh.j2",
        r"^flock\s+-w\s+(\d+)\s+9",
    ),
    "eval-run.sh.j2": (
        _INITIAL_SETUP_TEMPLATES / "eval-run.sh.j2",
        r"^flock\s+-w\s+(\d+)\s+9",
    ),
    "deploy_under_locks.py": (
        _REPO / "scripts" / "deploy_tools" / "deploy_under_locks.py",
        r"^LOCK_WAIT = (\d+)$",
    ),
}


def test_the_lock_waiter_census_is_non_vacuous():
    # Guards _LOCK_WAITERS itself. A census that silently lost a member — a renamed template, a
    # bad edit — would leave the parametrized test below iterating over whatever survived and
    # still passing, which is the failure this whole table exists to stop. Assert the names, not
    # a count, so the message says WHICH waiter went missing.
    assert set(_LOCK_WAITERS) == {
        "secret-rotate.sh.j2",
        "docs-refresh.sh.j2",
        "eval-run.sh.j2",
        "deploy_under_locks.py",
    }
    for name, (path, _) in _LOCK_WAITERS.items():
        assert path.is_file(), (
            f"{name}: {path} is gone; the census now checks nothing for it"
        )


@pytest.mark.parametrize("name", sorted(_LOCK_WAITERS))
def test_every_git_tree_lock_waiter_clears_the_deployers_worst_case_hold(name):
    # gitops-deploy.service wraps its whole ExecStart in /var/lock/server-git-tree.lock, and one
    # activation runs the staging gate, the expectation check, the forward deploy budget and
    # then, in the failure path, the rollback budget — sequentially, inside that one hold. A
    # waiter that gives up early does not fail safe: it fires its unit's OnFailure= alert for
    # ordinary contention AND skips that run. For the weekly secret-rotate that means the next
    # attempt is +7 days, and ROTATE_LEAD_DAYS=8 against a 7-day cadence means a token usually
    # gets exactly one eligible run, so a skipped week can put a token overdue.
    #
    # Derived from the same defaults the deployer reads, so bumping any of the four budgets
    # fails this instead of silently shortening every waiter at once.
    path, pattern = _LOCK_WAITERS[name]
    defaults = yaml.safe_load(_DEFAULTS.read_text())
    worst_hold = _worst_lock_hold(defaults)

    wait = int(_search1(pattern, path.read_text()))
    assert wait >= worst_hold, (
        f"{name}'s git-tree lock wait of {wait}s must clear gitops-deploy's worst-case lock "
        f"hold ({worst_hold}s: the staging gate and expectation check, then K8S_DEPLOY_TIMEOUT_S "
        f"and K8S_ROLLBACK_TIMEOUT_S), or a legitimate long rollback makes this job skip a run "
        f"and page for ordinary contention."
    )


def test_a_short_lock_waiter_is_flagged():
    # Red proof for the parametrized test above, which can only ever be observed passing. This
    # is the real pre-fix shape: docs-refresh's 2700 against the current 2940s hold.
    defaults = yaml.safe_load(_DEFAULTS.read_text())
    assert 2700 < _worst_lock_hold(defaults), (
        "the worst-case hold must exceed 2700 for this red proof to mean anything; if the "
        "budgets shrank below it, re-derive this number rather than deleting the test."
    )


# K8S_ROLLBACK_TIMEOUT_S must cover one full rollback cycle for the most expensive currently-
# promoted (k8s_autodeploy: true) service that also declares k8s_autodeploy_snapshot_pvcs: the
# pre-revert snapshot wait, the revert itself, whatever the role waits for in its OWN tasks, the
# forward apply's rollout wait, and the post-rollout stabilisation soak — all inside the SAME
# playbook run, on one continuous timeline
# where nothing fails (a failure aborts the whole play immediately, so it can never compound with
# an independent failure elsewhere — see docs/gitops-pipeline.md, *The rollback timeout, derived*).
#
# Deliberately a PER-SERVICE bound, not a per-batch one: co-batched claim-declaring services
# stack their snapshot+revert phases (only the rollout WAIT is deduped across a batch, via
# roles/k8s/rollout-drain), so a multi-service batch is NOT covered here — that gap is recorded
# at the DECIDED marker on the claim cap in deploy_k8s.py (the batch-abort blast radius) and in this same defaults/main.yml
# comment, deliberately not modeled by this test.
#
# Computed from role SOURCES, not pinned numbers, so a future rollout-timeout bump or a new
# promoted claim-declaring role fails this test instead of silently under-sizing the budget.

_K8S_ROLES_DIR = pathlib.Path(__file__).parents[3] / "k8s"
_ALL_VARS = pathlib.Path(__file__).parents[4] / "inventory" / "group_vars" / "all.yml"


def _rollout_timeout_s(role: str) -> int:
    # Shared with ansible/tests/longhorn/test_rollback_timeout_budget.py and the inline-gate
    # census. The literal read this replaced returned the shared default for a role that names
    # its budget in a variable (sonarr), sizing a 660s service as a 300s one with every test
    # green.
    return manifests_rollout_timeout_s(_K8S_ROLES_DIR / role)


# The role whose in-role wait this derivation must find. prowlarr's flaresolverr isolation probe
# waits `--timeout=300s` for a Job, before the batch drain runs and on top of it, and the
# derivation below counted only the drain until #2399. Named rather than counted: the reader
# finds its subject by pattern, so a rename or a moved task would otherwise leave the sum
# quietly smaller and every assertion here still green.
_IN_ROLE_WAIT_CENSUS = {"prowlarr": 300, "netpol-baseline": 650}


def test_the_in_role_wait_census_is_non_vacuous():
    for role, seconds in _IN_ROLE_WAIT_CENSUS.items():
        assert in_role_wait_s(role) >= seconds, (
            f"{role} no longer contributes {seconds}s of in-role waiting, so the budget "
            "derivations below are sizing against the drain alone again — find where that "
            "wait went before trusting a green run"
        )


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
        ceiling = (
            len(claims) * per_claim
            + in_role_wait_s(role)
            + _rollout_timeout_s(role)
            + stabilise
        )
        if ceiling > worst_ceiling:
            worst_role, worst_ceiling, worst_claims = role, ceiling, len(claims)

    assert worst_role is not None, (
        "no promoted (k8s_autodeploy: true), claim-declaring k8s role found — the sizing model "
        "this test encodes no longer matches the repo; update it rather than deleting it"
    )
    assert worst_ceiling <= rollback_timeout, (
        f"{worst_role} needs {worst_ceiling}s for one full rollback cycle "
        f"({worst_claims} claim(s), {in_role_wait_s(worst_role)}s of in-role waits, "
        f"{_rollout_timeout_s(worst_role)}s rollout), which exceeds "
        f"gitops_deploy_k8s_rollback_timeout_s ({rollback_timeout}s) — its rollback can be "
        f"SIGTERMed mid-revert. Raise that default (and TimeoutStartSec, and re-check this "
        f"test's own comment on the batch-summation gap it does not cover)."
    )


# The FORWARD deploy's own ceiling, derived the same way as the rollback's above and from the
# same role sources — minus the revert terms, which only the rollback run pays. One tick runs
# `ansible-playbook --tags <promoted services>` under `locked_budget(services,
# K8S_DEPLOY_TIMEOUT_S)`, and inside that one budget a promoted role pays, in sequence: the
# pre-apply snapshot wait per declared claim, its OWN in-role waits, the drain's rollout wait,
# and the stabilisation soak. The service-lock wait comes out of the same budget
# (`deploy_locks.locked_budget`), so it is overhead on top of all four.
#
# Every second the cap grows is also a second of `_worst_lock_hold` above, so the census of
# tree-lock waiters is what keeps a raise here from being done alone.
#
# EVERY promoted role is counted, not just the claim-declaring ones the rollback derivation
# narrows to: a claim-free role still pays the other three terms. That widening costs this
# derivation a guarantee the narrow one got for free — a claim-declaring role has a workload by
# construction, and a claim-free one may have none. `roles/k8s/manifests` queues the drain only
# when `manifests_rollout | default(manifests_service) | length > 0`, so a role passing
# `manifests_rollout: ""` waits for no rollout however long its own
# `manifests_rollout_timeout` reads. Counting the term anyway made netpol-baseline look like the
# worst role in the repo at 1310s when it pays 710s, and `manifests_rollout_timeout_s` cannot
# see the difference: it reads the budget, not whether anything spends it.
#
# The forward sum also carries two terms no role declares. Both are allowances rather than
# bounds, sized from the 2026-09-23 sonarr tick (27.8s in total, snapshot 4.74s, probe 0.27s):
#
#   - the service-lock wait, which `locked_budget` spends out of this same deadline. That tick
#     showed a 70s gap consistent with one. A wait longer than the allowance does not strand
#     anything: the run starts with less than it needs and, at worst, times out into
#     `_rollback_k8s`, the same route a rollout timeout takes.
#   - playbook overhead: start-up, fact gathering and renders, about 30s measured.
#
# The cap has to clear the worst role's ceiling plus both, or a slow prowlarr deploy is killed
# by the cap mid-drain before its own 780s rollout timeout can fire (#2397).
_FORWARD_LOCK_ALLOWANCE_S = 90
_FORWARD_OVERHEAD_S = 60

# The roles whose forward ceiling this derivation must keep reading the way it reads today. Both
# are found by pattern — the in-role waits by walking `tasks/`, the rollout term by looking for
# an empty `manifests_rollout` — so a rename or a moved task would leave the sum quietly smaller
# with every assertion below still green, the failure `_IN_ROLE_WAIT_CENSUS` above exists for.
#
#   prowlarr:        the worst promoted role, and the one the cap is sized against.
#   netpol-baseline: the role with the most in-role waiting in the repo and NO rollout to wait
#                    on. It is here because it is the shape that breaks the derivation, not
#                    because it is close to the cap.
_FORWARD_CEILING_CENSUS = {"prowlarr": 1260, "netpol-baseline": 710}


def _waits_for_a_rollout(role: str) -> bool:
    """Whether `roles/k8s/manifests` queues the drain for this role at all.

    False for a role that passes `manifests_rollout: ""` — NetworkPolicies, a PVC, a ConfigMap
    consumed by somebody else. Read off the role's own tasks rather than from the rendered
    manifests, because the empty override is what the queueing task's `when` reads.
    """
    tasks = _K8S_ROLES_DIR / role / "tasks" / "main.yml"
    text = tasks.read_text() if tasks.is_file() else ""
    return 'manifests_rollout: ""' not in text


def _forward_ceiling(
    role: str, role_defaults: dict, snapshot_timeout: int, stabilise: int
) -> int:
    """One promoted role's worst case inside K8S_DEPLOY_TIMEOUT_S, excluding the lock wait."""
    claims = role_defaults.get("k8s_autodeploy_snapshot_pvcs") or []
    rollout = _rollout_timeout_s(role) if _waits_for_a_rollout(role) else 0
    return len(claims) * snapshot_timeout + in_role_wait_s(role) + rollout + stabilise


def _worst_forward_case(snapshot_timeout: int, stabilise: int) -> tuple[str, int]:
    """The promoted role with the longest forward ceiling, and that ceiling."""
    worst_role, worst = "", 0
    for role_defaults_path in sorted(_K8S_ROLES_DIR.glob("*/defaults/main.yml")):
        role = role_defaults_path.parent.parent.name
        role_defaults = yaml.safe_load(role_defaults_path.read_text()) or {}
        if not role_defaults.get("k8s_autodeploy"):
            continue
        ceiling = _forward_ceiling(role, role_defaults, snapshot_timeout, stabilise)
        if ceiling > worst:
            worst_role, worst = role, ceiling
    return worst_role, worst


def _forward_terms() -> tuple[int, int, int]:
    """(snapshot timeout, stabilise soak, forward cap), each read from its own source."""
    snapshot_defaults = yaml.safe_load(
        (_K8S_ROLES_DIR / "volume-snapshot" / "defaults" / "main.yml").read_text()
    )
    all_vars = yaml.safe_load(_ALL_VARS.read_text())
    defaults = yaml.safe_load(_DEFAULTS.read_text())
    return (
        int(snapshot_defaults["volume_snapshot_timeout"]),
        int(all_vars["k8s_rollout_stabilise_seconds"]),
        int(defaults["gitops_deploy_k8s_timeout_s"]),
    )


def _forward_fits(worst: int, cap: int) -> bool:
    """Whether the worst role's forward ceiling, plus the lock allowance and overhead, fits the cap."""
    return _FORWARD_LOCK_ALLOWANCE_S + worst + _FORWARD_OVERHEAD_S <= cap


@pytest.mark.parametrize("role", sorted(_FORWARD_CEILING_CENSUS))
def test_the_forward_ceiling_census_is_non_vacuous(role):
    snapshot_timeout, stabilise, _ = _forward_terms()
    role_defaults = (
        yaml.safe_load((_K8S_ROLES_DIR / role / "defaults" / "main.yml").read_text())
        or {}
    )
    assert role_defaults.get("k8s_autodeploy"), (
        f"{role} is no longer promoted, so the derivation below never reaches it; find what "
        "the worst promoted role is now before trusting a green run"
    )
    ceiling = _forward_ceiling(role, role_defaults, snapshot_timeout, stabilise)
    assert ceiling == _FORWARD_CEILING_CENSUS[role], (
        f"{role}'s forward ceiling reads {ceiling}s, not the recorded "
        f"{_FORWARD_CEILING_CENSUS[role]}s ({in_role_wait_s(role)}s of in-role waits, rollout "
        f"{'counted' if _waits_for_a_rollout(role) else 'NOT counted'} at "
        f"{_rollout_timeout_s(role)}s). A moved wait or a changed `manifests_rollout` reads as "
        "a smaller sum here and leaves the fit check below green against a cap it outgrew."
    )


def test_the_forward_cap_covers_the_worst_promoted_role():
    snapshot_timeout, stabilise, cap = _forward_terms()
    worst_role, worst = _worst_forward_case(snapshot_timeout, stabilise)
    assert worst_role, (
        "no promoted (k8s_autodeploy: true) k8s role found — the sizing model this test "
        "encodes no longer matches the repo; update it rather than deleting it"
    )
    need = _FORWARD_LOCK_ALLOWANCE_S + worst + _FORWARD_OVERHEAD_S
    assert _forward_fits(worst, cap), (
        f"{worst_role} needs {need}s inside gitops_deploy_k8s_timeout_s ({cap}s): "
        f"{_FORWARD_LOCK_ALLOWANCE_S}s lock allowance + {in_role_wait_s(worst_role)}s of "
        f"in-role waits + {_rollout_timeout_s(worst_role)}s rollout + {stabilise}s soak + one "
        f"{snapshot_timeout}s snapshot per declared claim + {_FORWARD_OVERHEAD_S}s overhead. "
        f"A cap kill is a killpg MID-DRAIN that routes to _rollback_k8s (#2397). Raise the cap "
        f"WITH all four tree-lock waiters and the unit's TimeoutStartSec, or shrink the role's "
        f"waits; do not shrink the allowances to make the run green."
    )


def test_a_forward_ceiling_past_the_cap_is_caught():
    # Red proof for the fit check above, which can only ever be observed passing. Driven on the
    # verdict function the way `test_an_uncounted_staging_budget_is_caught` drives
    # `_budget_fits`. The pre-#2397 shape is the real one: prowlarr's 1260s against a 900s cap.
    assert _forward_fits(1260, 1440)
    assert not _forward_fits(1260, 900), (
        "the fit check must REJECT prowlarr's 1260s against the pre-#2397 900s cap; a check "
        "that passes it is measuring nothing"
    )
    assert not _forward_fits(1291, 1440), (
        "the fit check must count the lock allowance and the overhead, not the role terms alone"
    )
