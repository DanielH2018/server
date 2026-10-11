"""`fanout.py {place,probe,review,stats}` and the shims left at the four old paths (#4346).

Each entry point runs as a subprocess from a directory outside the repo, because a directly
invoked script gets only its own directory on `sys.path` and the suite's `pythonpath` would
hide a missing bootstrap.

Run: uv run pytest scripts/dev/tests/test_fanout_entrypoint.py
"""

import subprocess
import sys
from pathlib import Path

import pytest

import fanout

DEV = Path(__file__).resolve().parents[1]


def run(script: str, *argv: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(DEV / script), *argv],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        cwd=cwd,
    )


@pytest.mark.parametrize("command", sorted(fanout.COMMANDS))
def test_every_command_prints_its_own_help_from_outside_the_repo(command, tmp_path):
    done = run("fanout.py", command, "--help", cwd=tmp_path)
    assert done.returncode == 0, done.stderr[-2000:]
    assert done.stdout.startswith(f"usage: fanout.py {command} ")


# Each old path with arguments its command refuses, so the exit code is not 0 and a shim
# that dropped or reordered argv, or swallowed the code, prints or returns something else.
SHIM_CASES = [
    ("fanout_place.py", "place", ["status", "no-such-run-20000101T000000Z"], 1),
    ("fanout_probe.py", "probe", ["--describe", "no-such-run-20000101T000000Z"], 1),
    ("fanout_review.py", "review", ["--repo", "DanielH2018/server"], 2),
    ("fanout_review_stats.py", "stats", ["--since"], 2),
]


@pytest.mark.parametrize(
    ("shim", "command", "argv", "code"),
    SHIM_CASES,
    ids=[case[0] for case in SHIM_CASES],
)
def test_a_shim_forwards_argv_and_the_exit_code(shim, command, argv, code, tmp_path):
    if command == "place":
        argv = [*argv, "--manifest-root", str(tmp_path)]
    old = run(shim, *argv, cwd=tmp_path)
    new = run("fanout.py", command, *argv, cwd=tmp_path)
    assert (old.returncode, old.stdout, old.stderr) == (
        new.returncode,
        new.stdout,
        new.stderr,
    )
    assert old.returncode == code, old.stderr[-2000:]


def test_the_command_list_is_the_help_and_anything_else_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as helped:
        fanout.main(["--help"])
    assert helped.value.code == 0
    out = capsys.readouterr().out
    assert all(f"\n  {name} " in out for name in fanout.COMMANDS), out
    for argv in ([], ["bogus"], ["--batch", "1"]):
        with pytest.raises(SystemExit) as refused:
            fanout.main(argv)
        assert refused.value.code == 2, argv
