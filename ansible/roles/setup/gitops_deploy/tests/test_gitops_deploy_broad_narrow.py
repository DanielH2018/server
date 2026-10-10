"""A deploy-plane tick applies what the narrowing derived, or defers the plane when it refuses.

Each rule is a pair: the narrowed range, and a range the derivation refused. A refusal runs
no playbook at all since #4333: the whole play does not fit the tick's budget, so the plane
is merged and the services it may reach are recorded in `k8s_unapplied`. A narrowing that
fired on everything and one that fired on nothing read the same from the passing side alone.

The scripted narrowing is `tick.narrow`, an `(exit code, stdout)` pair — the same contract
`deploy_tags.py narrow` has, and the boundary `DeployTools.narrow_deploy_plane` crosses. Its
default is a refusal.

Run: uv run pytest ansible/roles/setup/gitops_deploy/tests/test_gitops_deploy_broad_narrow.py
"""

import dataclasses

import deploy_narrow
import deploy_tick_types
from _deploy_fakes import receipt_applied
from gitops_ledger import OWED_K8S_UNAPPLIED

ORIGIN = "2" * 40
# A deploy-plane path: inventory is in `_BROAD_DEPLOY_PREFIXES`, and the tick used to run
# `ansible/deploy.yml` unscoped for it.
GROUP_VARS = "ansible/inventory/group_vars/all.yml"
DECLARES_RADARR_SONARR = (
    "containers_list:\n  - name: radarr\n    platform: k8s\n"
    "  - name: sonarr\n    platform: k8s\n"
)


def _playbook_argv(tick):
    """The one ansible-playbook argv this tick ran; fails when it ran none or several."""
    assert len(tick.playbooks) == 1, tick.playbooks
    return tick.playbooks[0]


def test_a_narrowed_range_deploys_only_the_tags_it_named(gitops_deploy, tick, state):
    tick.paths = [GROUP_VARS]
    tick.narrow = (0, "radarr,sonarr")
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert _playbook_argv(tick)[-3:] == [
        "ansible/deploy.yml",
        "--tags",
        "radarr,sonarr",
    ]
    assert tick.merges == [ORIGIN]
    assert receipt_applied(state) == {"ansible/deploy.yml": ("radarr", "sonarr")}


def _owed(state) -> list[str]:
    return sorted(e.service for e in state.owed_pending(OWED_K8S_UNAPPLIED))


def test_a_refused_range_runs_no_play_and_owes_every_declared_service(
    gitops_deploy, tick, capsys, state
):
    """#4333: an untagged deploy.yml ran past the tick's budget and held every landing.

    No receipt either: an empty tag list there reads as "the whole play ran" to land.sh.
    """
    tick.declare(DECLARES_RADARR_SONARR)
    tick.paths = [GROUP_VARS]
    tick.narrow = (3, "")
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert tick.playbooks == []
    assert tick.merges == [ORIGIN]
    assert receipt_applied(state) is None
    assert state.hold_plane is None
    assert _owed(state) == ["radarr", "sonarr"]
    out = capsys.readouterr().out
    assert "cannot narrow (exit 3)" in out
    assert "deferring every declared service to k8s_unapplied" in out


def test_a_refusal_naming_its_tags_owes_only_those(gitops_deploy, tick, state):
    """The fleet-coverage ceiling knows what it reached, so the rest owe nothing."""
    tick.declare(DECLARES_RADARR_SONARR)
    tick.paths = [GROUP_VARS]
    tick.narrow = (3, "sonarr,wg-easy")
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert tick.playbooks == []
    assert _owed(state) == ["sonarr"]


def test_a_range_that_moves_no_rendered_output_applies_nothing(
    gitops_deploy, tick, state
):
    """Exit 0 with no tags: the fast-forward IS the apply, and the marker says so.

    `--tags ''` would run the whole playbook and `--tags <a name nothing matches>` would run
    every `tags: always` task in it, so neither spelling means "nothing" to Ansible.
    """
    tick.paths = [GROUP_VARS]
    tick.narrow = (0, "")
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert tick.playbooks == []
    assert tick.merges == [ORIGIN]
    assert receipt_applied(state) == {"ansible/deploy.yml": ("narrowed-to-nothing",)}


def test_a_failed_narrowed_apply_holds_the_tags_it_tried(
    gitops_deploy, tick, state_dir, state
):
    """The hold names the narrowed plane, so only an apply covering those tags clears it."""
    tick.paths = [GROUP_VARS]
    tick.narrow = (0, "sonarr")
    tick.playbook_outcomes = [RuntimeError("boom")]
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert (state_dir / "hold_sha").read_text() == ORIGIN
    assert state.hold_plane == "ansible/deploy.yml sonarr"


def test_the_setup_plane_never_consults_the_deploy_plane_narrowing(
    gitops_deploy, tick, state
):
    """The two narrowings are separate derivations over separate playbooks.

    `setup_tags_for` maps a setup path to its role tag and `narrow_setup_role` narrows that
    (#3120, `test_gitops_deploy_setup_narrow.py`). Asking `deploy_tags.py narrow` about a
    setup range would route it through `_deploy_plane`, whose refusal branch runs a full
    `ansible/deploy.yml` for a change that reaches no container.
    """
    tick.paths = ["ansible/roles/setup/gitops_deploy/tasks/main.yml"]
    tick.narrow = (0, "sonarr")
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert not [entry for entry in tick.log if entry[0] == "narrow"]
    assert _playbook_argv(tick)[-3:] == [
        "ansible/initial_setup.yml",
        "--tags",
        "gitops_deploy",
    ]


def test_a_mixed_range_applies_the_setup_plane_and_then_the_deploy_plane(
    gitops_deploy, tick, state
):
    """Both planes in one range, both applied, setup first (#2046).

    Until 2026-09-18 the planner was an if/else and the setup arm won: the deploy half of a
    mixed range was never planned, so a removed Pi `containers_list` entry's full-run
    fallback — the re-stamp `Release Staleness Drift` relies on — never ran.
    """
    tick.paths = ["ansible/roles/setup/gitops_deploy/tasks/main.yml", GROUP_VARS]
    tick.narrow = (0, "sonarr")
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert [argv[-3:] for argv in tick.playbooks] == [
        ["ansible/initial_setup.yml", "--tags", "gitops_deploy"],
        ["ansible/deploy.yml", "--tags", "sonarr"],
    ]
    assert tick.merges == [ORIGIN]
    assert receipt_applied(state) == {
        "ansible/initial_setup.yml": ("gitops_deploy",),
        "ansible/deploy.yml": ("sonarr",),
    }


def test_a_mixed_range_whose_deploy_half_is_refused_applies_only_the_setup_plane(
    gitops_deploy, tick, state
):
    """The setup apply still runs and is recorded; the deploy half is owed, not run."""
    tick.declare(DECLARES_RADARR_SONARR)
    tick.paths = ["ansible/roles/setup/gitops_deploy/tasks/main.yml", GROUP_VARS]
    tick.narrow = (3, "")
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert [argv[-1] for argv in tick.playbooks] == ["gitops_deploy"]
    assert receipt_applied(state) == {"ansible/initial_setup.yml": ("gitops_deploy",)}
    assert _owed(state) == ["radarr", "sonarr"]


def test_a_failed_setup_apply_still_owes_the_refused_deploy_plane(
    gitops_deploy, tick, state
):
    """The failure arm returns before `apply_broad_k8s`, so it records the deferral itself."""
    tick.declare(DECLARES_RADARR_SONARR)
    tick.paths = ["ansible/roles/setup/gitops_deploy/tasks/main.yml", GROUP_VARS]
    tick.narrow = (3, "radarr")
    tick.playbook_outcomes = [RuntimeError("boom")]
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert state.hold_plane == "ansible/initial_setup.yml gitops_deploy"
    assert _owed(state) == ["radarr"]


def test_a_mixed_range_whose_deploy_half_fails_keeps_the_setup_apply_recorded(
    gitops_deploy, tick, state
):
    """The hold names the plane that failed; the plane that applied stays recorded."""
    tick.paths = ["ansible/roles/setup/gitops_deploy/tasks/main.yml", GROUP_VARS]
    tick.narrow = (0, "sonarr")
    tick.playbook_outcomes = [None, RuntimeError("boom")]
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert state.hold_plane == "ansible/deploy.yml sonarr"
    assert receipt_applied(state) == {"ansible/initial_setup.yml": ("gitops_deploy",)}


def test_a_setup_only_range_plans_no_deploy_plane(gitops_deploy, tick, state):
    """The rejecting half: no deploy-plane path means no `deploy.yml`, narrowed or full."""
    tick.paths = ["ansible/roles/setup/gitops_deploy/tasks/main.yml"]
    tick.narrow = (3, "")
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert [argv[-1] for argv in tick.playbooks] == ["gitops_deploy"]


def test_a_crashing_narrowing_defers_the_plane(gitops_deploy, tick, capsys, state):
    """The `# DECIDED:` marker says a crash here lands on the fallback, so prove it can.

    `narrow_deploy_plane` decodes a subprocess's output, so it can raise a plain ValueError
    (`UnicodeDecodeError`) as well as a `SubprocessError`. `plan` runs before the ff-merge,
    so an escaping exception reaches the entrypoint handler and parks every later landing.
    """
    tick.paths = [GROUP_VARS]
    tick.narrow_error = ValueError("invalid start byte")
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert tick.playbooks == []
    assert tick.merges == [ORIGIN]
    assert "cannot narrow (ValueError: invalid start byte)" in capsys.readouterr().out


def _denylisted_plan(settings, out: str, capsys):
    """`deploy_narrow.plan` over a deploy-plane range narrowing to `out`, with crowdsec and
    traefik denylisted; returns the plan and what the journal said."""
    config = dataclasses.replace(
        settings, k8s_autodeploy_denylist=frozenset({"crowdsec", "traefik"})
    )
    target = deploy_tick_types.TickTarget(
        local="1" * 40,
        origin=ORIGIN,
        hold=None,
        dirty=False,
        status="",
        action="deploy",
    )
    plan = deploy_narrow.plan(
        lambda *_: (0, out), config, target, set(), True, lambda *_: (1, ""), {}
    )
    return plan, capsys.readouterr().out


def test_a_narrowed_range_applies_its_denylisted_tags_and_names_them(settings, capsys):
    """The denylist gates k8s auto-deploy promotion, not the broad plane (issue #1962).

    The `# DECIDED:` on `deploy_narrow.denylisted_in` is the reasoning; this pins the
    behaviour it settles: every tag the range reaches is applied, denied or not, and the
    journal says which were denied ones. A filter landing here fails this test on purpose.
    """
    plan, out = _denylisted_plan(settings, "crowdsec,traefik,radarr", capsys)
    assert plan == [
        deploy_narrow.BroadPlan(
            "ansible/deploy.yml", ["crowdsec", "traefik", "radarr"], True
        )
    ]
    assert "narrow: crowdsec,traefik are denylisted for k8s auto-deploy" in out
    assert "radarr are denylisted" not in out


def test_a_narrowed_range_with_no_denylisted_tag_logs_no_denylist_line(
    settings, capsys
):
    """The rejecting half of the line above: it fires on a denied tag, not on every apply."""
    plan, out = _denylisted_plan(settings, "radarr,sonarr", capsys)
    assert plan == [
        deploy_narrow.BroadPlan("ansible/deploy.yml", ["radarr", "sonarr"], True)
    ]
    assert "denylisted for k8s auto-deploy" not in out


def test_the_denylist_decision_is_recorded_where_the_narrowing_reads_it(gitops_src):
    """The marker names the identifier, so a reader grepping for the denylist finds it.

    Asserts the identifier and the marker, never the paragraph: a reword must not fail this,
    and a marker moved out of the module that decides must.
    """
    text = (gitops_src.parent / "deploy_narrow.py").read_text()
    assert "K8S_AUTODEPLOY_DENYLIST" in text
    assert "# DECIDED: the broad plane ignores `K8S_AUTODEPLOY_DENYLIST`" in text


# ── the render-digest shadow (#3045): logged beside the answer, never applied ──────────────
def test_a_refused_range_logs_the_digest_shadow_and_defers_the_plane(
    gitops_deploy, tick, capsys, state
):
    """The shadow names what a digest diff would apply; the tick runs nothing regardless."""
    tick.paths = [GROUP_VARS]
    tick.narrow = (3, "")
    tick.tools = dataclasses.replace(
        tick.tools,
        digest_diff=lambda _ref: {
            "drifted": ["radarr"],
            "current": ["sonarr"],
            "unknown: render is of another commit": ["authelia", "traefik"],
        },
    )
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert tick.playbooks == []
    assert (
        "narrow shadow: render digest at 22222222 would apply radarr (current 1; "
        "unknown: render is of another commit 2); the narrowing chose to defer the plane"
    ) in capsys.readouterr().out


def test_a_digest_diff_that_raises_leaves_the_narrowed_apply_alone(
    gitops_deploy, tick, capsys, state
):
    """The shadow runs before the ff-merge, so a crash in it must not reach the tick."""

    def broken(_ref):
        raise OSError("permission denied")

    tick.paths = [GROUP_VARS]
    tick.narrow = (0, "radarr,sonarr")
    tick.tools = dataclasses.replace(tick.tools, digest_diff=broken)
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert _playbook_argv(tick)[-1:] == ["radarr,sonarr"]
    assert tick.merges == [ORIGIN]
    assert (
        "narrow shadow: no digest diff (OSError: permission denied)"
        in capsys.readouterr().out
    )
