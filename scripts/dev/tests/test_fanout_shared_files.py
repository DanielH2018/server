"""The file-level collision check (#1798): two batches citing one file are refused at launch."""

from fanout_lib.brief import Issue
from fanout_lib.collisions import shared_files
from fanout_place import main
from _fanout_fakes import HOST_KEY, fake_tools, ok

HEADROOM = f"1\n12884901888\n1\n12884901888\n0\n{HOST_KEY}\n"

# The 2026-09-11 shape: three issues under one domain label, two batches, one shared script.
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
            "--orchestrator-branch",
            "o",
            "--manifest-root",
            str(tmp_path),
        ],
        tools,
    )


def test_shared_files_names_the_file_and_every_batch_that_cites_it():
    batches = {"1780-1784": [1780, 1784], "1782": [1782]}
    issues = {i.number: i for i in ISSUES}
    assert shared_files(batches, issues) == [(ALERTS, ["1780-1784", "1782"])]


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
    assert "regroup them into one batch" in err
    assert not list(tmp_path.glob("*.json"))


def test_launch_proceeds_when_the_shared_script_is_grouped_into_one_batch(tmp_path):
    tools, run = fake_tools(answers={"daniel-box": ok(HEADROOM)}, issues=ISSUES)
    assert _launch(tools, tmp_path, "--batch", "1780,1782", "--batch", "1784") == 0
    assert [c for c in run.calls if "worktree add" in c[1]]


def test_allow_shared_files_launches_anyway_and_still_prints_the_collision(
    tmp_path, capsys
):
    tools, run = fake_tools(answers={"daniel-box": ok(HEADROOM)}, issues=ISSUES)
    assert (
        _launch(
            tools,
            tmp_path,
            "--batch",
            "1780,1784",
            "--batch",
            "1782",
            "--allow-shared-files",
        )
        == 0
    )
    err = capsys.readouterr().err
    assert f"all cite {ALERTS}" in err
    assert "--allow-shared-files set, launching anyway" in err
    assert [c for c in run.calls if "worktree add" in c[1]]


def test_the_triage_step_names_the_file_level_collision_check():
    """The issue's verify-by: the rule lives in the skill the orchestrator reads, and the
    `Done when:` checklist is what it ticks off, so both have to name it or it is skipped."""
    from lib.repo_paths import REPO

    skill = (REPO / ".claude/skills/issue-fanout/SKILL.md").read_text()
    triage = skill[skill.index("## 1. Triage") : skill.index("## 2.")]
    assert "file-level collision check" in triage
    assert "`paths`" in triage
    assert "--allow-shared-files" in triage
    assert "no two batches share a cited file" in triage
