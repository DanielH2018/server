"""A setup-plane tick applies the role's block tags, or its role tag when it cannot (#3120).

The arm this covers used to run `initial_setup.yml --tags <role>` for every setup-plane
change. For `initial_setup` that tag selects about 440 tasks, even when the diff touches one
template a single block tag reaches. PR #3116 gave each of those tasks a tag narrower than
`crons`, so `scripts/deploy_tools/narrow_setup.py` can derive which ones the diff needs.

Each rule is a pair, the way `test_gitops_deploy_broad_narrow.py`'s are: a range the
derivation narrowed, and one it refused, which must run byte-for-byte what this arm ran
before. A narrowing that fired on everything and one that fired on nothing read the same from
the passing side alone.

`tick.narrow_setup` is the scripted derivation, keyed by ROLE directory, each value the
`(exit code, stdout)` pair `narrow_setup.py` prints. A role absent from it answers `(1, "")`,
which is the refusal every test written before this slice relies on.

Run: uv run pytest ansible/roles/setup/gitops_deploy/tests/test_gitops_deploy_setup_narrow.py
"""

ORIGIN = "2" * 40

# A setup-plane path under one role that `initial_setup.yml` applies. A template rather than a
# `tasks/` file, because a template is the shape the narrowing exists for: one block tag
# renders it, and the whole-role tag reapplies every other block beside it.
GITOPS_TEMPLATE = "ansible/roles/setup/gitops_deploy/templates/config.env.j2"
RENOVATE_TEMPLATE = "ansible/roles/setup/renovate_notify/templates/notify.sh.j2"
# `ansible/requirements.yml` is the setup-plane path that maps to no role directory at all —
# `sops_setup` installs the collections, and the tag is `collections`.
REQUIREMENTS = "ansible/requirements.yml"


def _playbook_argv(tick):
    """The one ansible-playbook argv this tick ran; fails when it ran none or several."""
    assert len(tick.playbooks) == 1, tick.playbooks
    return tick.playbooks[0]


def test_a_narrowed_setup_role_applies_its_block_tags(gitops_deploy, tick):
    tick.paths = [GITOPS_TEMPLATE]
    tick.narrow_setup = {"gitops_deploy": (0, "gitops-config,gitops-unit")}
    assert gitops_deploy.main(tick.tools) == 0
    assert _playbook_argv(tick)[-3:] == [
        "ansible/initial_setup.yml",
        "--tags",
        "gitops-config,gitops-unit",
    ]
    assert tick.merges == [ORIGIN]


def test_a_refused_setup_role_still_applies_the_whole_role_tag(
    gitops_deploy, tick, capsys
):
    """The rejecting half: exit 1 is what the arm did for every setup range before #3120."""
    tick.paths = [GITOPS_TEMPLATE]
    tick.narrow_setup = {"gitops_deploy": (1, "")}
    assert gitops_deploy.main(tick.tools) == 0
    assert _playbook_argv(tick)[-3:] == [
        "ansible/initial_setup.yml",
        "--tags",
        "gitops_deploy",
    ]
    out = capsys.readouterr().out
    assert "cannot narrow gitops_deploy (exit 1)" in out
    assert "applying --tags gitops_deploy" in out


def test_an_exception_in_the_derivation_applies_the_whole_role_tag(
    gitops_deploy, tick, capsys
):
    """`plan` runs before the ff-merge, so an escape here would park every landing."""
    tick.paths = [GITOPS_TEMPLATE]
    tick.narrow_setup_error = TimeoutError("the child outlived its budget")
    assert gitops_deploy.main(tick.tools) == 0
    assert _playbook_argv(tick)[-3:] == [
        "ansible/initial_setup.yml",
        "--tags",
        "gitops_deploy",
    ]
    assert "TimeoutError: the child outlived its budget" in capsys.readouterr().out


def test_exit_zero_with_no_tags_applies_the_whole_role_tag(gitops_deploy, tick):
    """An empty `--tags` value runs the WHOLE playbook, so an empty answer is doubt."""
    tick.paths = [GITOPS_TEMPLATE]
    tick.narrow_setup = {"gitops_deploy": (0, "")}
    assert gitops_deploy.main(tick.tools) == 0
    assert _playbook_argv(tick)[-3:] == [
        "ansible/initial_setup.yml",
        "--tags",
        "gitops_deploy",
    ]


def test_one_role_narrowing_does_not_widen_another_that_refused(gitops_deploy, tick):
    """PER ROLE, not all-or-nothing: the narrowed tags sit beside the refused role's tag."""
    tick.paths = [GITOPS_TEMPLATE, RENOVATE_TEMPLATE]
    tick.narrow_setup = {"gitops_deploy": (0, "gitops-config")}
    assert gitops_deploy.main(tick.tools) == 0
    assert _playbook_argv(tick)[-3:] == [
        "ansible/initial_setup.yml",
        "--tags",
        "gitops-config,renovate_notify",
    ]


def test_a_tag_belonging_to_no_role_passes_through(gitops_deploy, tick):
    """`collections` is `requirements.yml`'s tag, mapped to no role directory.

    Nothing can derive it, and dropping it would leave the Galaxy collections uninstalled
    with nothing said about it.
    """
    tick.paths = [REQUIREMENTS, GITOPS_TEMPLATE]
    tick.narrow_setup = {"gitops_deploy": (0, "gitops-config")}
    assert gitops_deploy.main(tick.tools) == 0
    assert _playbook_argv(tick)[-3:] == [
        "ansible/initial_setup.yml",
        "--tags",
        "collections,gitops-config",
    ]


def test_the_derivation_is_asked_about_the_role_and_this_ticks_range(
    gitops_deploy, tick
):
    """One call per appliable role, carrying the role directory rather than its tag.

    `narrow_setup.py` takes the role DIRECTORY; `chezmoi_setup` is tagged `chezmoi`, so a call
    built from the tag would ask about a directory that does not exist and refuse every time —
    a regression that reads as "the narrowing never fires" rather than as a bug.
    """
    tick.paths = [GITOPS_TEMPLATE]
    tick.narrow_setup = {"gitops_deploy": (0, "gitops-config")}
    assert gitops_deploy.main(tick.tools) == 0
    asked = [entry[1] for entry in tick.log if entry[0] == "narrow_setup"]
    assert asked == [["gitops_deploy", "ansible/initial_setup.yml", tick.local, ORIGIN]]


def test_a_role_this_deployer_cannot_apply_is_not_narrowed(gitops_deploy, tick):
    """`setup/k3s` lives in `k3s-bringup.yml`, so it is recorded for a human, not narrowed.

    `deploy_defer.record` runs its own derivation for the marker that human reads. Narrowing
    it here as well would narrow a command this tick never runs.
    """
    tick.paths = ["ansible/roles/setup/k3s/templates/readonly-rbac.yaml.j2"]
    assert gitops_deploy.main(tick.tools) == 0
    asked = [entry[1][0] for entry in tick.log if entry[0] == "narrow_setup"]
    assert asked == ["k3s"]
    assert tick.playbooks == []


def test_a_failed_narrowed_setup_apply_holds_the_role_tag_it_narrowed_from(
    gitops_deploy, tick, state_dir
):
    """The hold names the ROLE, not the block tags the apply ran.

    `broad_hold_cleared_by` compares tag strings, so a hold naming `gitops-config` would
    survive a later `--tags gitops_deploy` apply that reruns that very block. The paired
    clear-side test below is the half that makes this one load-bearing.
    """
    tick.paths = [GITOPS_TEMPLATE]
    tick.narrow_setup = {"gitops_deploy": (0, "gitops-config")}
    tick.playbook_outcomes = [RuntimeError("boom")]
    assert gitops_deploy.main(tick.tools) == 0
    assert _playbook_argv(tick)[-1] == "gitops-config", (
        "it still APPLIES the narrow tags"
    )
    assert (state_dir / "hold_sha").read_text() == ORIGIN
    assert (
        state_dir / "hold_plane"
    ).read_text() == "ansible/initial_setup.yml gitops_deploy"


def test_the_whole_role_fallback_clears_a_hold_a_narrowed_apply_left(
    gitops_deploy, tick, state_dir
):
    """The way out: the apply that fixes a held setup role clears the hold.

    A sticky `hold_sha` parks every session's landing, so the hold a narrowed apply writes
    has to be one a later apply of the same role covers.
    """
    (state_dir / "hold_sha").write_text("1" * 40)
    (state_dir / "hold_plane").write_text("ansible/initial_setup.yml gitops_deploy")
    tick.paths = [GITOPS_TEMPLATE]
    tick.narrow_setup = {"gitops_deploy": (1, "")}
    assert gitops_deploy.main(tick.tools) == 0
    assert _playbook_argv(tick)[-1] == "gitops_deploy"
    assert not (state_dir / "hold_sha").exists()
    assert not (state_dir / "hold_plane").exists()


def test_the_narrowing_loop_stops_asking_once_its_budget_is_spent():
    """One shared budget for the loop, not one per role.

    `plan` runs before the ff-merge and inside the unit's `TimeoutStartSec`, which the phase
    budgets in `defaults/main.yml` already fill. Nothing bounds how many setup roles a range
    carries, so without a shared deadline a wide range spends the per-role budget once per
    role before anything is applied.
    """
    import deploy_narrow

    asked = []
    # The deadline, then one reading per role: the second is past the budget.
    clock = iter([0.0, 0.0, deploy_narrow.NARROW_SETUP_TOTAL_BUDGET_S + 1.0])

    def narrow_setup(repo, role, role_tag, playbook, old, new, timeout):
        asked.append(role)
        return 0, "gitops-config"

    config = type("C", (), {"repo": "/repo"})()
    target = type("T", (), {"local": "1" * 40, "origin": ORIGIN})()
    tags = deploy_narrow.narrowed_setup_tags(
        narrow_setup,
        config,
        target,
        {"gitops_deploy", "renovate_notify"},
        {"gitops_deploy": "gitops_deploy", "renovate_notify": "renovate_notify"},
        now=lambda: next(clock),
    )
    assert asked == ["gitops_deploy"], "the second role must not be asked"
    assert tags == ["gitops-config", "renovate_notify"]
