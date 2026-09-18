"""A deploy-plane tick applies what the narrowing derived, or the whole play when it refuses.

Each rule is a pair: the narrowed range, and a range the derivation refused, which must run
byte-for-byte what this arm ran before the narrowing existed. A narrowing that fired on
everything and one that fired on nothing read the same from the passing side alone.

The scripted narrowing is `tick.narrow`, an `(exit code, stdout)` pair — the same contract
`deploy_tags.py narrow` has, and the boundary `DeployTools.narrow_deploy_plane` crosses. Its
default is a refusal, so every test written before this slice keeps asserting the full run.

Run: uv run pytest ansible/roles/setup/gitops_deploy/tests/test_gitops_deploy_broad_narrow.py
"""

import dataclasses

import deploy_narrow
import deploy_tick_types

ORIGIN = "2" * 40
# A deploy-plane path: inventory is in `_BROAD_DEPLOY_PREFIXES`, and the tick used to run
# `ansible/deploy.yml` unscoped for it.
GROUP_VARS = "ansible/inventory/group_vars/all.yml"


def _playbook_argv(tick):
    """The one ansible-playbook argv this tick ran; fails when it ran none or several."""
    assert len(tick.playbooks) == 1, tick.playbooks
    return tick.playbooks[0]


def test_a_narrowed_range_deploys_only_the_tags_it_named(gitops_deploy, tick):
    tick.paths = [GROUP_VARS]
    tick.narrow = (0, "radarr,sonarr")
    assert gitops_deploy.main(tick.tools) == 0
    assert _playbook_argv(tick)[-3:] == [
        "ansible/deploy.yml",
        "--tags",
        "radarr,sonarr",
    ]
    assert tick.merges == [ORIGIN]
    assert (
        gitops_deploy.STATE.broad_applied
        == f"{ORIGIN} ansible/deploy.yml radarr,sonarr"
    )


def test_a_refused_range_still_runs_the_whole_play(gitops_deploy, tick, capsys):
    """The rejecting half: exit 3 is what the tick did for every deploy-plane range before."""
    tick.paths = [GROUP_VARS]
    tick.narrow = (3, "")
    assert gitops_deploy.main(tick.tools) == 0
    assert _playbook_argv(tick) == [
        "uv",
        "run",
        "--frozen",
        "ansible-playbook",
        "ansible/deploy.yml",
    ]
    assert "cannot narrow (exit 3)" in capsys.readouterr().out
    assert gitops_deploy.STATE.broad_applied == f"{ORIGIN} ansible/deploy.yml"


def test_a_range_that_moves_no_rendered_output_applies_nothing(gitops_deploy, tick):
    """Exit 0 with no tags: the fast-forward IS the apply, and the marker says so.

    `--tags ''` would run the whole playbook and `--tags <a name nothing matches>` would run
    every `tags: always` task in it, so neither spelling means "nothing" to Ansible.
    """
    tick.paths = [GROUP_VARS]
    tick.narrow = (0, "")
    assert gitops_deploy.main(tick.tools) == 0
    assert tick.playbooks == []
    assert tick.merges == [ORIGIN]
    assert (
        gitops_deploy.STATE.broad_applied
        == f"{ORIGIN} ansible/deploy.yml narrowed-to-nothing"
    )


def test_a_failed_narrowed_apply_holds_the_tags_it_tried(
    gitops_deploy, tick, state_dir
):
    """The hold names the narrowed plane, so only an apply covering those tags clears it."""
    tick.paths = [GROUP_VARS]
    tick.narrow = (0, "sonarr")
    tick.playbook_outcomes = [RuntimeError("boom")]
    assert gitops_deploy.main(tick.tools) == 0
    assert (state_dir / "hold_sha").read_text() == ORIGIN
    assert (state_dir / "hold_plane").read_text() == "ansible/deploy.yml sonarr"


def test_the_setup_plane_never_consults_the_narrowing(gitops_deploy, tick):
    """`setup_tags_for` already derived that plane's tags; this arm must not re-derive them."""
    tick.paths = ["ansible/roles/setup/gitops_deploy/tasks/main.yml"]
    tick.narrow = (0, "sonarr")
    assert gitops_deploy.main(tick.tools) == 0
    assert not [entry for entry in tick.log if entry[0] == "narrow"]
    assert _playbook_argv(tick)[-3:] == [
        "ansible/initial_setup.yml",
        "--tags",
        "gitops_deploy",
    ]


def test_a_crashing_narrowing_still_runs_the_whole_play(gitops_deploy, tick, capsys):
    """The `# DECIDED:` marker says a crash here lands on the fallback, so prove it can.

    `narrow_deploy_plane` decodes a subprocess's output, so it can raise a plain ValueError
    (`UnicodeDecodeError`) as well as a `SubprocessError`. `plan` runs before the ff-merge,
    so an escaping exception reaches the entrypoint handler and parks every later landing.
    """
    tick.paths = [GROUP_VARS]
    tick.narrow_error = ValueError("invalid start byte")
    assert gitops_deploy.main(tick.tools) == 0
    assert _playbook_argv(tick)[-1:] == ["ansible/deploy.yml"]
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
    plan = deploy_narrow.plan(lambda *_: (0, out), config, target, set())
    return plan, capsys.readouterr().out


def test_a_narrowed_range_applies_its_denylisted_tags_and_names_them(settings, capsys):
    """The denylist gates k8s auto-deploy promotion, not the broad plane (issue #1962).

    The `# DECIDED:` on `deploy_narrow.denylisted_in` is the reasoning; this pins the
    behaviour it settles: every tag the range reaches is applied, denied or not, and the
    journal says which were denied ones. A filter landing here fails this test on purpose.
    """
    plan, out = _denylisted_plan(settings, "crowdsec,traefik,radarr", capsys)
    assert plan == deploy_narrow.BroadPlan(
        "ansible/deploy.yml", ["crowdsec", "traefik", "radarr"], True
    )
    assert "narrow: crowdsec,traefik are denylisted for k8s auto-deploy" in out
    assert "radarr are denylisted" not in out


def test_a_narrowed_range_with_no_denylisted_tag_logs_no_denylist_line(
    settings, capsys
):
    """The rejecting half of the line above: it fires on a denied tag, not on every apply."""
    plan, out = _denylisted_plan(settings, "radarr,sonarr", capsys)
    assert plan == deploy_narrow.BroadPlan(
        "ansible/deploy.yml", ["radarr", "sonarr"], True
    )
    assert "denylisted for k8s auto-deploy" not in out


def test_the_denylist_decision_is_recorded_where_the_narrowing_reads_it(gitops_src):
    """The marker names the identifier, so a reader grepping for the denylist finds it.

    Asserts the identifier and the marker, never the paragraph: a reword must not fail this,
    and a marker moved out of the module that decides must.
    """
    text = (gitops_src.parent / "deploy_narrow.py").read_text()
    assert "K8S_AUTODEPLOY_DENYLIST" in text
    assert "# DECIDED: the broad plane ignores `K8S_AUTODEPLOY_DENYLIST`" in text
