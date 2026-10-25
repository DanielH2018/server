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

import subprocess

from deployer_lib import deploy_narrow
from deployer_lib.deploy_setup_roles import SECRET_READERS_SCRIPT, SecretReadersRefused
from gitops_ledger import OWED_HOLD_PLANE, owed_line

from _deploy_fakes import receipt_applied

ORIGIN = "2" * 40

# A setup-plane path under one role that `initial_setup.yml` applies. A template rather than a
# `tasks/` file, because a template is the shape the narrowing exists for: one block tag
# renders it, and the whole-role tag reapplies every other block beside it.
GITOPS_TEMPLATE = "ansible/roles/setup/gitops_deploy/templates/config.env.j2"
RENOVATE_TEMPLATE = "ansible/roles/setup/renovate_notify/templates/notify.sh.j2"
# `ansible/requirements.yml` is the setup-plane path that maps to no role directory at all —
# `sops_setup` installs the collections, and the tag is `collections`.
REQUIREMENTS = "ansible/requirements.yml"
# Another role's defaults file a setup role reads one key of, so every edit to it names that
# role beside the owner (#4303): claude_code here, and deploy_ui for k3s's.
INITIAL_SETUP_DEFAULTS = "ansible/roles/setup/initial_setup/defaults/main.yml"
K3S_DEFAULTS = "ansible/roles/setup/k3s/defaults/main.yml"
# What `narrow_setup.py` answers for a role the range reaches no host through (#4361).
NOTHING = (deploy_narrow.NARROW_SETUP_NOTHING_EXIT, deploy_narrow.NARROWED_TO_NOTHING)


def _playbook_argv(tick):
    """The one ansible-playbook argv this tick ran; fails when it ran none or several."""
    assert len(tick.playbooks) == 1, tick.playbooks
    return tick.playbooks[0]


def test_a_narrowed_setup_role_applies_its_block_tags(gitops_deploy, tick, state):
    tick.paths = [GITOPS_TEMPLATE]
    tick.narrow_setup = {"gitops_deploy": (0, "gitops-config,gitops-unit")}
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert _playbook_argv(tick)[-3:] == [
        "ansible/initial_setup.yml",
        "--tags",
        "gitops-config,gitops-unit",
    ]
    assert tick.merges == [ORIGIN]


def test_a_refused_setup_role_still_applies_the_whole_role_tag(
    gitops_deploy, tick, capsys, state
):
    """The rejecting half: exit 1 is what the arm did for every setup range before #3120."""
    tick.paths = [GITOPS_TEMPLATE]
    tick.narrow_setup = {"gitops_deploy": (1, "")}
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert _playbook_argv(tick)[-3:] == [
        "ansible/initial_setup.yml",
        "--tags",
        "gitops_deploy",
    ]
    out = capsys.readouterr().out
    assert "cannot narrow gitops_deploy (exit 1)" in out
    assert "applying --tags gitops_deploy" in out


def test_an_exception_in_the_derivation_applies_the_whole_role_tag(
    gitops_deploy, tick, capsys, state
):
    """`plan` runs before the ff-merge, so an escape here would park every landing."""
    tick.paths = [GITOPS_TEMPLATE]
    tick.narrow_setup_error = TimeoutError("the child outlived its budget")
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert _playbook_argv(tick)[-3:] == [
        "ansible/initial_setup.yml",
        "--tags",
        "gitops_deploy",
    ]
    assert "TimeoutError: the child outlived its budget" in capsys.readouterr().out


def test_exit_zero_with_no_tags_applies_the_whole_role_tag(gitops_deploy, tick, state):
    """An empty `--tags` value runs the WHOLE playbook, so an empty answer is doubt."""
    tick.paths = [GITOPS_TEMPLATE]
    tick.narrow_setup = {"gitops_deploy": (0, "")}
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert _playbook_argv(tick)[-3:] == [
        "ansible/initial_setup.yml",
        "--tags",
        "gitops_deploy",
    ]


def test_one_role_narrowing_does_not_widen_another_that_refused(
    gitops_deploy, tick, state
):
    """PER ROLE, not all-or-nothing: the narrowed tags sit beside the refused role's tag."""
    tick.paths = [GITOPS_TEMPLATE, RENOVATE_TEMPLATE]
    tick.narrow_setup = {"gitops_deploy": (0, "gitops-config")}
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert _playbook_argv(tick)[-3:] == [
        "ansible/initial_setup.yml",
        "--tags",
        "gitops-config,renovate_notify",
    ]


def test_a_tag_belonging_to_no_role_passes_through(gitops_deploy, tick, state):
    """`collections` is `requirements.yml`'s tag, mapped to no role directory.

    Nothing can derive it, and dropping it would leave the Galaxy collections uninstalled
    with nothing said about it.
    """
    tick.paths = [REQUIREMENTS, GITOPS_TEMPLATE]
    tick.narrow_setup = {"gitops_deploy": (0, "gitops-config")}
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert _playbook_argv(tick)[-3:] == [
        "ansible/initial_setup.yml",
        "--tags",
        "collections,gitops-config",
    ]


def test_the_derivation_is_asked_about_the_role_and_this_ticks_range(
    gitops_deploy, tick, state
):
    """One call per appliable role, carrying the role directory rather than its tag.

    `narrow_setup.py` takes the role DIRECTORY; `chezmoi_setup` is tagged `chezmoi`, so a call
    built from the tag would ask about a directory that does not exist and refuse every time —
    a regression that reads as "the narrowing never fires" rather than as a bug.
    """
    tick.paths = [GITOPS_TEMPLATE]
    tick.narrow_setup = {"gitops_deploy": (0, "gitops-config")}
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    asked = [entry[1] for entry in tick.log if entry[0] == "narrow_setup"]
    assert asked == [["gitops_deploy", "ansible/initial_setup.yml", tick.local, ORIGIN]]


def test_a_role_this_deployer_cannot_apply_is_not_narrowed(gitops_deploy, tick, state):
    """`setup/k3s` lives in `k3s-bringup.yml`, so it is recorded for a human, not narrowed.

    `deploy_defer.record` runs its own derivation for the marker that human reads. Narrowing
    it here as well would narrow a command this tick never runs.
    """
    tick.paths = ["ansible/roles/setup/k3s/templates/readonly-rbac.yaml.j2"]
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    asked = [entry[1][0] for entry in tick.log if entry[0] == "narrow_setup"]
    assert asked == ["k3s"]
    assert tick.playbooks == []


def test_a_failed_narrowed_setup_apply_holds_the_blocks_it_ran_with_their_role(
    gitops_deploy, tick, state_dir, state
):
    """The hold names the block tags the apply ran, each qualified by its role (#3138).

    A bare `gitops-config` would survive a later `--tags gitops_deploy` apply that reruns
    that very block. The role qualifier is what lets the whole-role apply clear it, and the
    paired clear-side tests below are the half that makes this one load-bearing.
    """
    tick.paths = [GITOPS_TEMPLATE]
    tick.narrow_setup = {"gitops_deploy": (0, "gitops-config")}
    tick.playbook_outcomes = [RuntimeError("boom")]
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert _playbook_argv(tick)[-1] == "gitops-config", (
        "it still APPLIES the narrow tags"
    )
    assert (state_dir / "hold_sha").read_text() == ORIGIN
    assert state.hold_plane == "ansible/initial_setup.yml gitops_deploy:gitops-config"


def test_a_narrowed_apply_of_another_block_keeps_the_hold(
    gitops_deploy, tick, state_dir, state
):
    """A held block stays held until something reruns it.

    Before #3138 the hold named the whole role, so an apply narrowed to a DIFFERENT block of
    that role matched nothing and kept it anyway, while a hold naming only the block it ran
    could not be cleared by the role's whole-role apply. Now the different block keeps it
    for the right reason, and the next test clears it.
    """
    held = "ansible/initial_setup.yml gitops_deploy:gitops-config"
    (state_dir / "hold_sha").write_text("1" * 40)
    (state_dir / "owed.jsonl").write_text(owed_line(OWED_HOLD_PLANE, held, "1" * 40, 1))
    tick.paths = [GITOPS_TEMPLATE]
    tick.narrow_setup = {"gitops_deploy": (0, "gitops-timer")}
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert _playbook_argv(tick)[-1] == "gitops-timer"
    assert (state_dir / "hold_sha").read_text() == "1" * 40
    assert state.hold_plane == held


def test_the_whole_role_fallback_clears_a_hold_a_narrowed_apply_left(
    gitops_deploy, tick, state_dir, state
):
    """The way out: the apply that fixes a held setup role clears the hold.

    A sticky `hold_sha` parks every session's landing, so the hold a narrowed apply writes
    has to be one a later apply of the same role covers.
    """
    (state_dir / "hold_sha").write_text("1" * 40)
    (state_dir / "owed.jsonl").write_text(
        owed_line(
            OWED_HOLD_PLANE,
            "ansible/initial_setup.yml gitops_deploy:gitops-config",
            "1" * 40,
            1,
        )
    )
    tick.paths = [GITOPS_TEMPLATE]
    tick.narrow_setup = {"gitops_deploy": (1, "")}
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert _playbook_argv(tick)[-1] == "gitops_deploy"
    assert not (state_dir / "hold_sha").exists()
    assert state.hold_plane is None


def test_the_narrowing_loop_stops_asking_once_its_budget_is_spent():
    """One shared budget for the loop, not one per role.

    `plan` runs before the ff-merge and inside the unit's `TimeoutStartSec`, which the phase
    budgets in `defaults/main.yml` already fill. Nothing bounds how many setup roles a range
    carries, so without a shared deadline a wide range spends the per-role budget once per
    role before anything is applied.
    """

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
    assert tags == {
        "gitops_deploy": ["gitops-config"],
        "renovate_notify": ["renovate_notify"],
    }


def test_a_shipped_file_applies_and_holds_every_role_that_ships_it(
    gitops_deploy, tick, state
):
    """deploy_ui and renovate_agent install the deployer's markers by path (#3306).

    A range touching only that file applies all three roles' blocks, and a failed apply holds
    each block under its own role, so no apply of the owner alone can clear the hold.
    """
    tick.paths = ["ansible/roles/setup/gitops_deploy/files/gitops_markers.py"]
    tick.narrow_setup = {
        "gitops_deploy": (0, "gitops-deploy-code"),
        "deploy_ui": (0, "deploy-ui-code"),
        "renovate_agent": (0, "renovate-agent-code"),
    }
    tick.playbook_outcomes = [RuntimeError("boom")]
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert _playbook_argv(tick)[-1] == (
        "deploy-ui-code,gitops-deploy-code,renovate-agent-code"
    )
    assert state.hold_plane == (
        "ansible/initial_setup.yml deploy_ui:deploy-ui-code,"
        "gitops_deploy:gitops-deploy-code,renovate_agent:renovate-agent-code"
    )
    assert state.manual_plane_pending() == []


def test_a_role_needing_no_tag_is_left_out_of_the_apply_and_the_hold(
    gitops_deploy, tick, state
):
    """An initial_setup default claude_code never reads plans no claude_code tag (#4361).

    The row names claude_code for every edit to the file. Before this answer the tick applied
    the whole `claude_code` tag, and a failure there held the deployer over a change
    claude_code never read.
    """
    tick.paths = [INITIAL_SETUP_DEFAULTS]
    tick.narrow_setup = {
        "claude_code": NOTHING,
        "homelab_builder": NOTHING,
        "renovate_agent": NOTHING,
        "initial_setup": (0, "prune"),
    }
    tick.playbook_outcomes = [RuntimeError("boom")]
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert _playbook_argv(tick)[-1] == "prune"
    assert state.hold_plane == "ansible/initial_setup.yml initial_setup:prune"


def test_exit_three_without_the_nothing_line_applies_the_whole_role_tag(
    gitops_deploy, tick, state
):
    """The rejecting half: the exit code alone is not the answer, so it stays doubt."""
    tick.paths = [INITIAL_SETUP_DEFAULTS]
    tick.narrow_setup = {
        "claude_code": (deploy_narrow.NARROW_SETUP_NOTHING_EXIT, ""),
        "homelab_builder": NOTHING,
        "renovate_agent": NOTHING,
        "initial_setup": (0, "prune"),
    }
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert _playbook_argv(tick)[-1] == "claude_code,prune"


def test_a_range_no_setup_role_needs_a_tag_for_runs_no_playbook(
    gitops_deploy, tick, state
):
    """An empty `--tags` value runs the whole playbook, so nothing runs and the receipt says so.

    A k3s default deploy_ui never reads: k3s is recorded for a hand, and deploy_ui, the one
    role the tick would apply, needs nothing.
    """
    tick.paths = [K3S_DEFAULTS]
    tick.narrow_setup = {"deploy_ui": NOTHING}
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert tick.playbooks == []
    assert tick.merges == [ORIGIN]
    assert receipt_applied(state) == {
        "ansible/initial_setup.yml": (deploy_narrow.NARROWED_TO_NOTHING,)
    }
    assert [e.role for e in state.manual_plane_pending()] == ["k3s"]


# ── a secrets.yml key a setup role reads (#5436) ──────────────────────────────────────────────
SECRETS = "ansible/vars/secrets.yml"
CLAUDE_TRIAGE_TASKS = "ansible/roles/setup/claude_code/tasks/triage.yml"


def test_a_secrets_only_range_applies_the_setup_role_reading_its_key(
    gitops_deploy, tick, state
):
    """PR #5405's shape: one new key `claude_code` reads, and no path under the role.

    Before #5436 the range took the no-services branch, so it merged, paged, and applied
    nothing. The page still goes out, because no derivation sees a consumer it refused.
    """
    tick.paths = [SECRETS]
    tick.secret_readers = [CLAUDE_TRIAGE_TASKS]
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert _playbook_argv(tick)[-3:] == [
        "ansible/initial_setup.yml",
        "--tags",
        "claude_code",
    ]
    assert tick.merges == [ORIGIN]
    assert any("`secrets.yml` changed" in post for post in tick.posts)


def test_a_reader_another_playbook_applies_is_recorded_not_applied(
    gitops_deploy, tick, state
):
    """A key only k3s reads: `k3s-bringup.yml` applies k3s, so the tick records it for a hand."""
    tick.paths = [SECRETS]
    tick.secret_readers = ["ansible/roles/setup/k3s/templates/registries.yaml.j2"]
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert tick.playbooks == []
    assert tick.merges == [ORIGIN]
    assert [e.role for e in state.manual_plane_pending()] == ["k3s"]


def test_a_refused_reader_derivation_merges_and_applies_no_setup_role(
    gitops_deploy, tick, capsys, state
):
    """A refusal routes nothing, so the range behaves as before #5436: merged and paged."""
    tick.paths = [SECRETS]
    tick.secret_readers = [CLAUDE_TRIAGE_TASKS]
    tick.secret_readers_error = SecretReadersRefused("cannot say (a play-level reader)")
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert tick.playbooks == []
    assert tick.merges == [ORIGIN]
    assert any("`secrets.yml` changed" in post for post in tick.posts)
    assert "no setup role routed" in capsys.readouterr().out


def test_a_reader_derivation_that_times_out_parks_the_range(
    gitops_deploy, tick, capsys, state
):
    """A timeout is not an answer: merged, no later range would carry the key again."""
    tick.paths = [SECRETS]
    tick.secret_readers = [CLAUDE_TRIAGE_TASKS]
    tick.secret_readers_error = subprocess.TimeoutExpired(["uv"], 30)
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert tick.merges == [] and tick.playbooks == []
    assert "parked, nothing merged" in capsys.readouterr().out


def test_a_shipper_that_reads_none_of_the_secrets_keys_is_not_applied(
    gitops_deploy, tick, state
):
    """claude_code's defaults name four secrets, and initial_setup ships the file for one
    unrelated key. Without `unread` the rotation applied all of `--tags initial_setup`."""
    claude_defaults = "ansible/roles/setup/claude_code/defaults/main.yml"
    tick.paths = [SECRETS]
    tick.secret_readers = [claude_defaults]
    tick.secret_unread = {claude_defaults: ["initial_setup"]}
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert _playbook_argv(tick)[-1] == "claude_code"


def _park_on_a_failed_reader_lookup(gitops_deploy, tick, state, error):
    """One tick over a `secrets.yml` range whose reader lookup raises `error`."""
    tick.paths = [SECRETS]
    tick.secret_readers = [CLAUDE_TRIAGE_TASKS]
    tick.secret_readers_error = error
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert tick.merges == []


def test_a_parked_reader_failure_logs_the_lookup_as_the_reason(
    gitops_deploy, tick, capsys, state
):
    """The journal reason is load-bearing (#1467): it must not read as the nameless-path park."""
    _park_on_a_failed_reader_lookup(
        gitops_deploy, tick, state, OSError("uv: not found")
    )
    parked = [ln for ln in capsys.readouterr().out.splitlines() if "park:" in ln]
    assert len(parked) == 1, parked
    assert "secret-reader lookup failed" in parked[0]
    assert "names no role" not in parked[0]


def test_a_parked_reader_failure_pages_the_retry_and_never_a_merge(
    gitops_deploy, tick, capsys, state
):
    """An operator who merges past the `secrets.yml` change loses the key for good."""
    _park_on_a_failed_reader_lookup(
        gitops_deploy, tick, state, OSError("uv: not found")
    )
    pages = [post for post in tick.posts if ORIGIN[:8] in post]
    assert len(pages) == 1, tick.posts
    assert "git merge --ff-only" not in pages[0]
    assert "next tick retries" in pages[0]
    assert SECRET_READERS_SCRIPT in pages[0]
    assert "git merge --ff-only" not in capsys.readouterr().out


def test_a_transient_reader_failure_is_retried_by_the_next_tick(
    gitops_deploy, tick, state
):
    """The park leaves the range unmerged, so the next tick looks the readers up again."""
    _park_on_a_failed_reader_lookup(
        gitops_deploy, tick, state, subprocess.TimeoutExpired(["uv"], 30)
    )
    assert tick.playbooks == []
    tick.secret_readers_error = None
    assert gitops_deploy.main(tick.tools, tick.config, state) == 0
    assert tick.merges == [ORIGIN]
    assert _playbook_argv(tick)[-1] == "claude_code"
