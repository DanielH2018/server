"""`fanout.py {place,probe,review,stats}` from outside the repo.

Each command runs as a subprocess from a directory outside the repo, because a directly
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


def test_the_command_list_is_the_help_and_anything_else_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as helped:
        fanout.main(["--help"])
    assert helped.value.code == 0
    out = capsys.readouterr().out
    assert all(f"\n  {name} " in out for name in fanout.COMMANDS), out
    for argv in ([], ["bogus"], ["--batch", "1"]):
        with pytest.raises(SystemExit) as refused:
            fanout.main(argv)
        assert refused.value.code == 64, argv
