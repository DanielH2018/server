"""`reconcile_denylist`: the deployer re-rendering its own config.env when it goes stale.

`K8S_AUTODEPLOY_DENYLIST` is derived from every role under roles/k8s/ at RENDER time, and only
`initial_setup.yml --tags gitops_deploy` renders it. A change under roles/k8s/ matches no prefix
that runs that playbook, so adding a role declaring `k8s_autodeploy: false` left the baked list
stale and disarmed image-pin auto-deploy FLEET-WIDE — measured on game-stats-lib, 12:30 to 18:29
UTC on 2026-09-05 (issues #1265, #1294). This phase closes that gap by stating the invariant
locally: config.env must agree with the declarations at the checkout's own HEAD.

Both halves of every rule are here, because a check is only ever observed passing: a case it
must re-render for, and a case it must leave alone.

Run: uv run pytest ansible/roles/setup/gitops_deploy/tests/test_denylist_reconcile.py
"""

import dataclasses

import deploy_phases

LOCAL = "1" * 40
K8S_DEFAULTS = "ansible/roles/k8s/sonarr/defaults/main.yml"
RENDER_CONFIG = [
    "uv",
    "run",
    "--frozen",
    "ansible-playbook",
    "ansible/initial_setup.yml",
    "--tags",
    "gitops_deploy",
    "-e",
    "gitops_deploy_kick_after_change=false",
]


def _armed(settings, denylist=()):
    """`settings` with auto-deploy on and `denylist` baked into the host config."""
    return dataclasses.replace(
        settings,
        k8s_autodeploy_enabled=True,
        k8s_autodeploy_denylist=frozenset(denylist),
    )


def _declares(tick, stance: str) -> None:
    """One k8s role at the checkout's HEAD, declaring `stance`."""
    tick.tree_listing = K8S_DEFAULTS + "\n"
    tick.files[f"{LOCAL}:{K8S_DEFAULTS}"] = f"k8s_autodeploy: {stance}\n"


def _marker(state_dir, name: str) -> str | None:
    path = state_dir / name
    return path.read_text().strip() if path.exists() else None


# ── the phase on its own ──────────────────────────────────────────────────────────────────
def test_a_config_that_disagrees_with_the_checkout_is_re_rendered(
    gitops_deploy, tick, state_dir, settings
):
    """The flagged half: HEAD denies a role the baked config does not — the #1294 shape."""
    _declares(tick, "false")
    assert deploy_phases.reconcile_denylist(
        gitops_deploy.STATE, _armed(settings), LOCAL
    )
    assert tick.playbooks == [RENDER_CONFIG]
    assert gitops_deploy.STATE.read("denylist_rendered") == LOCAL


def test_a_config_that_already_matches_renders_nothing(
    gitops_deploy, tick, state_dir, settings
):
    """The clean half. Same scripted checkout, config that agrees with it — no playbook."""
    _declares(tick, "false")
    assert not deploy_phases.reconcile_denylist(
        gitops_deploy.STATE, _armed(settings, ["sonarr"]), LOCAL
    )
    assert tick.playbooks == []
    assert gitops_deploy.STATE.read("denylist_rendered") == LOCAL


def test_the_same_checkout_is_read_once_and_only_once(
    gitops_deploy, tick, state_dir, settings
):
    """The once-per-SHA guard: no re-render, and not even the per-role `git show` reads."""
    _declares(tick, "false")
    gitops_deploy.STATE.write("denylist_rendered", LOCAL)
    assert not deploy_phases.reconcile_denylist(
        gitops_deploy.STATE, _armed(settings), LOCAL
    )
    assert tick.playbooks == [] and tick.git == []


def test_a_failed_re_render_marks_the_sha_and_does_not_park_the_deployer(
    gitops_deploy, tick, state_dir, settings
):
    """A render that fails must not retry every tick, and must not hold the whole pipeline.

    The marker is written BEFORE the run for the first half; no `hold_sha` for the second — a
    failed render leaves the OLD config, which is the state the tick already tolerates, with
    auto-deploy disarmed by the origin comparison in `_promote_k8s_auto_deploys`.
    """
    _declares(tick, "false")
    tick.playbook_outcomes = [RuntimeError("ansible exploded")]
    assert deploy_phases.reconcile_denylist(
        gitops_deploy.STATE, _armed(settings), LOCAL
    )
    assert gitops_deploy.STATE.read("denylist_rendered") == LOCAL
    assert gitops_deploy.STATE.hold_sha is None


def test_an_unreadable_ref_retries_next_tick_instead_of_claiming_the_sha(
    gitops_deploy, tick, state_dir, settings
):
    """A transient git failure is not evidence about the denylist, so it marks no SHA."""
    tick.tree_listing = K8S_DEFAULTS + "\n"  # nothing scripted for `git show`
    assert not deploy_phases.reconcile_denylist(
        gitops_deploy.STATE, _armed(settings), LOCAL
    )
    assert tick.playbooks == []
    assert gitops_deploy.STATE.read("denylist_rendered") is None


def test_it_is_inert_while_auto_deploy_is_off(gitops_deploy, tick, state_dir, settings):
    """Nothing reads the denylist when the feature is off, so nothing renders for it."""
    _declares(tick, "false")
    assert not deploy_phases.reconcile_denylist(gitops_deploy.STATE, settings, LOCAL)
    assert tick.playbooks == [] and tick.git == []


# ── the same thing through a whole tick ───────────────────────────────────────────────────
def test_an_idle_tick_re_renders_a_stale_denylist(
    gitops_deploy, tick, state_dir, settings
):
    """The heal lands on the tick AFTER the fast-forward, which is a converged, otherwise-idle
    one — exactly the tick that used to return 0 having done nothing while auto-deploy stayed
    disarmed fleet-wide."""
    _declares(tick, "false")
    tick.origin = tick.local
    assert gitops_deploy.main(tick.tools, _armed(settings, ["other"])) == 0
    assert tick.playbooks == [RENDER_CONFIG]
    assert _marker(state_dir, "denylist_rendered_sha") == LOCAL


def test_a_render_ends_the_tick_before_anything_deploys(
    gitops_deploy, tick, state_dir, settings
):
    """No arm of this unit stacks its budget on another's — the unit template sizes
    TimeoutStartSec as max(broad, staging + k8s + rollback) rather than a sum — and the config
    in memory still holds the list the render just disproved. So a render is terminal."""
    _declares(tick, "false")
    tick.paths = [K8S_DEFAULTS]
    assert gitops_deploy.main(tick.tools, _armed(settings, ["other"])) == 0
    assert tick.playbooks == [RENDER_CONFIG]
    assert tick.merges == [] and tick.posts == []


def test_a_tick_that_renders_nothing_carries_on(
    gitops_deploy, tick, state_dir, settings
):
    """The other half: the early return is the render's, not the reconcile's."""
    _declares(tick, "false")
    tick.paths = ["docs/runbook.md"]
    assert gitops_deploy.main(tick.tools, _armed(settings, ["sonarr"])) == 0
    assert tick.playbooks == []
    assert tick.merges == [tick.origin], "a docs-only push still fast-forwards"


def test_a_dirty_checkout_is_never_rendered_from(
    gitops_deploy, tick, state_dir, settings
):
    """The reject half of the placement: the render derives the denylist from the WORKING TREE,
    so rendering mid-edit would bake a list nobody pushed."""
    _declares(tick, "false")
    tick.origin = tick.local
    tick.dirty = True
    assert gitops_deploy.main(tick.tools, _armed(settings, ["other"])) == 0
    assert tick.playbooks == []
    assert _marker(state_dir, "denylist_rendered_sha") is None
