"""The file-level collision check: two batches citing one file are refused at launch."""

from fanout_lib.brief import Issue
from fanout_lib.launch_gates import shared_files
from fanout_lib.place import main
from _fanout_fakes import HOST_KEY, fake_tools, ok

HEADROOM = f"1\n12884901888\n1\n12884901888\n0\n{HOST_KEY}\n"

# The shape: three issues under one domain label, two batches, one shared script.
ALERTS = "scripts/diagnostics/probe_lib/alerts.py"
ISSUES = [
    Issue(
        1780, "bridge", f"`{ALERTS}` and ansible/roles/k8s/monitor-bridge/", ("claude",)
    ),
    Issue(
        1784,
        "discord",
        "ansible/roles/k8s/monitor-bridge/defaults/main.yml",
        ("claude",),
    ),
    Issue(1782, "probe", f'  File "{ALERTS}", line 336, in run_alerts', ("claude",)),
]


def _launch(tools, tmp_path, *args):
    return main(
        [
            "launch",
            *args,
            "--host",
            "daniel-box",
            "--manifest-root",
            str(tmp_path),
        ],
        tools,
    )


def test_shared_files_names_the_file_and_every_batch_that_cites_it():
    batches = {"1780-1784": [1780, 1784], "1782": [1782]}
    issues = {i.number: i for i in ISSUES}
    assert shared_files(batches, issues) == [(ALERTS, ["1780-1784", "1782"])]


def test_a_fragment_collides_with_the_full_path_it_names():
    """#4232: `probe_lib/alerts.py` and the full path are one file to two agents."""
    issues = {
        1: Issue(1, "a", f"`{ALERTS}` drops the row", ("claude",)),
        2: Issue(2, "b", "`probe_lib/alerts.py:336` drops it too", ("claude",)),
    }
    batches = {"1": [1], "2": [2]}
    assert shared_files(batches, issues, tracked={ALERTS}) == [(ALERTS, ["1", "2"])]


def test_shared_files_is_clean_when_the_shared_script_sits_in_one_batch():
    batches = {"1780-1782": [1780, 1782], "1784": [1784]}
    issues = {i.number: i for i in ISSUES}
    assert shared_files(batches, issues) == []


def test_launch_refuses_two_batches_that_cite_one_file_before_any_ssh(tmp_path, capsys):
    tools, run = fake_tools(answers={"daniel-box": ok(HEADROOM)}, issues=ISSUES)
    assert _launch(tools, tmp_path, "--batch", "1780,1784", "--batch", "1782") == 1
    assert not run.calls
    err = capsys.readouterr().err
    assert f"batches 1780-1784, 1782 all cite {ALERTS}" in err
    assert f"regroup them into one batch, or pass --allow-shared-file {ALERTS}" in err
    assert not list(tmp_path.glob("*.json"))


def test_launch_proceeds_when_the_shared_script_is_grouped_into_one_batch(tmp_path):
    tools, run = fake_tools(answers={"daniel-box": ok(HEADROOM)}, issues=ISSUES)
    assert _launch(tools, tmp_path, "--batch", "1780,1782", "--batch", "1784") == 0
    assert [c for c in run.calls if "worktree add" in c[1]]


def test_allow_shared_file_excuses_the_named_file_only(tmp_path, capsys):
    # A doc path is context for both batches; the override names it and nothing else, so the
    # script collision beside it still refuses.
    doc = "docs/claude-tooling.md"
    issues = [
        Issue(1780, "bridge", f"`{ALERTS}` and `{doc}`", ("claude",)),
        Issue(1782, "probe", f"`{ALERTS}` and `{doc}`", ("claude",)),
    ]
    tools, run = fake_tools(answers={"daniel-box": ok(HEADROOM)}, issues=issues)
    assert (
        _launch(
            tools,
            tmp_path,
            "--batch",
            "1780",
            "--batch",
            "1782",
            "--allow-shared-file",
            doc,
        )
        == 1
    )
    err = capsys.readouterr().err
    assert f"all cite {doc} — allowed by --allow-shared-file" in err
    assert f"all cite {ALERTS} — two agents editing one file" in err
    assert not run.calls

    tools, run = fake_tools(answers={"daniel-box": ok(HEADROOM)}, issues=issues)
    assert (
        _launch(
            tools,
            tmp_path,
            "--batch",
            "1780",
            "--batch",
            "1782",
            "--allow-shared-file",
            doc,
            "--allow-shared-file",
            ALERTS,
        )
        == 0
    )
    assert [c for c in run.calls if "worktree add" in c[1]]


def test_launch_refuses_a_batch_holding_a_fanout_tooling_issue_before_any_ssh(
    tmp_path, capsys
):
    issues = [
        *ISSUES,
        Issue(3959, "tooling", "`fanout_place.py launch` refuses", ("claude",)),
    ]
    tools, run = fake_tools(answers={"daniel-box": ok(HEADROOM)}, issues=issues)
    assert _launch(tools, tmp_path, "--batch", "1784,3959") == 1
    assert not run.calls
    err = capsys.readouterr().err
    assert "batch 1784-3959: #3959 cites the fan-out tooling (fanout_place.py)" in err
    assert not list(tmp_path.glob("*.json"))


def test_the_triage_step_names_the_file_level_collision_check():
    """The rule lives in the skill the orchestrator reads, so its triage step has to name
    the field the check reads and the flag that overrides it, or the check is skipped."""
    from lib.repo_paths import REPO

    skill = (REPO / ".claude/skills/issue-fanout/SKILL.md").read_text()
    triage = skill[skill.index("## 1. Triage") : skill.index("## 2.")]
    assert "`paths`" in triage
    assert "--allow-shared-file" in triage
