"""A deploy-plane tick applies what the narrowing derived, or the whole play when it refuses.

Each rule is a pair: the narrowed range, and a range the derivation refused, which must run
byte-for-byte what this arm ran before the narrowing existed. A narrowing that fired on
everything and one that fired on nothing read the same from the passing side alone.

The scripted narrowing is `tick.narrow`, an `(exit code, stdout)` pair — the same contract
`deploy_tags.py narrow` has, and the boundary `DeployTools.narrow_deploy_plane` crosses. Its
default is a refusal, so every test written before this slice keeps asserting the full run.

Run: uv run pytest ansible/roles/setup/gitops_deploy/tests/test_gitops_deploy_broad_narrow.py
"""

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
