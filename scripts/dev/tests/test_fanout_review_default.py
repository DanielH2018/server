"""Which unit `launch` starts: the review pipeline by default, the bare agent on `--no-review`.

Run: uv run pytest scripts/dev/tests/test_fanout_review_default.py
"""

from fanout_lib.brief import Issue
from fanout_lib.place import main
from _fanout_fakes import HOST_KEY, fake_tools, ok

# Fleet current, fleet cap, login-plane current, login-plane cap, live agents, signing key.
HEADROOM = f"1\n12884901888\n1\n12884901888\n0\n{HOST_KEY}\n"


def _unit_command(tmp_path, *flags) -> str:
    tools, run = fake_tools(
        answers={"daniel-box": ok(HEADROOM)},
        issues=[Issue(1, "one", "body one", ("claude",))],
    )
    argv = ["launch", "--batch", "1", "--host", "daniel-box", *flags]
    assert main([*argv, "--manifest-root", str(tmp_path)], tools) == 0
    (launch_call,) = [c for c in run.host_calls if "systemd-run" in c[1]]
    return launch_call[1]


def test_a_launch_with_no_review_flag_runs_the_review_unit(tmp_path):
    """Review is the default (#3675): a bare launch starts `fanout.py review`."""
    assert "fanout.py review --batch 1" in _unit_command(tmp_path)


def test_no_review_runs_the_bare_agent_instead(tmp_path):
    assert "fanout.py review" not in _unit_command(tmp_path, "--no-review")
