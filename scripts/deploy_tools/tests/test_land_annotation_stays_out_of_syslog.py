"""The `_no_syslog` fixture must actually intercept land.sh's annotation.

Run: uv run pytest scripts/deploy_tools/tests/test_land_annotation_stays_out_of_syslog.py

A fixture that stops being on PATH — because a test module builds its own env from scratch,
or because `land.sh` starts calling `logger` by absolute path — fails OPEN: the run stays
green and the annotations quietly reach syslog again. That is the exact shape the repo's
"a new check ships with a proof it can go RED" rule exists for, so the assertions below are a
pair: one that the stub captured land.sh's real annotation, one that the annotation is the
landing line rather than any stray `logger` call.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

_LAND_SH = Path(__file__).resolve().parents[1] / "land.sh"
# What `Options.primary` defaults to. Nothing here may run in it.
_LIVE_PRIMARY = Path("/home/ubuntu/server")


def _stub_bin(tmp_path: Path) -> Path:
    """A `gh` that reports PR 939 already merged, and a `git` that only records being called.

    `Tools.gh_json` parses stdout with `json.loads`, so the stub answers in JSON: the bash
    `--jq` tab format made every run of this module die at the first `gh pr view` with
    "unparseable gh output", short of the already-merged path it exists to exercise.

    `{}` for every other read is what ends the run: `--arm-merge` no-ops on the MERGED PR,
    then step 1 reads no merge commit and dies. That is before `fetch_branch`, so a git call
    recorded here would mean the landing went somewhere this test never intended it to.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (tmp_path / "gh-calls").touch()
    (tmp_path / "git-calls").touch()

    gh = bin_dir / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\t%s\\n" "$PWD" "$*" >> "{tmp_path}/gh-calls"\n'
        'case "$*" in\n'
        '  *"--json state,title"*)\n'
        '    printf \'{"state":"MERGED","title":"Already merged"}\\n\' ;;\n'
        "  *)\n"
        "    printf '{}\\n' ;;\n"
        "esac\n"
    )
    gh.chmod(0o755)

    git = bin_dir / "git"
    git.write_text(
        f'#!/bin/sh\nprintf "%s\\t%s\\n" "$PWD" "$*" >> "{tmp_path}/git-calls"\n'
    )
    git.chmod(0o755)
    return bin_dir


def _run_land(tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """One landing against the stubs, with `LAND_PRIMARY` aimed at tmp_path."""
    bin_dir = _stub_bin(tmp_path)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "LAND_PRIMARY": str(tmp_path),
    }
    return subprocess.run(
        ["bash", str(_LAND_SH), "--pr", "939", "--arm-merge"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_land_annotation_is_intercepted(tmp_path, logger_calls):
    """The stub caught the landing line, so nothing reached the host's syslog.

    This is the non-vacuity half: `logger_calls` being empty would mean either that land.sh
    never annotated (so the fixture proves nothing) or that the real `logger` took the call.
    Both are failures.
    """
    result = _run_land(tmp_path)

    assert "unparseable gh output" not in result.stderr, result.stderr
    assert "already merged; --arm-merge is a no-op" in result.stdout, result.stdout

    captured = logger_calls.read_text()
    assert captured.strip(), (
        "the stubbed logger recorded nothing — either land.sh no longer annotates, or the "
        "real /usr/bin/logger took the call and this run reached syslog"
    )
    assert "event=landing" in captured
    assert "pr=939" in captured

    # This module's landings must reach no checkout at all: the run ends at the merge-commit
    # read, which is before `fetch_branch`. Whether LAND_PRIMARY aims a landing that DOES
    # reach git is pinned by test_land_arm_merge_through_the_shim.py, which runs that far.
    gh_calls = (tmp_path / "gh-calls").read_text().splitlines()
    assert gh_calls, (
        "the gh stub recorded nothing, so this proves nothing about where it ran"
    )
    assert str(_LIVE_PRIMARY) not in {line.split("\t")[0] for line in gh_calls}
    assert (tmp_path / "git-calls").read_text() == "", (
        "the landing reached git, which this module's stubs do not answer for"
    )


def test_the_stub_is_what_resolves_for_logger(logger_calls):
    """`logger` on PATH is the stub, not the host binary.

    The rejecting half. If the fixture's PATH mutation stopped taking effect, this resolves to
    /usr/bin/logger and fails, rather than every other test in the directory going quietly
    back to writing syslog.
    """
    resolved = subprocess.run(
        ["sh", "-c", "command -v logger"], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert "logger-stub" in resolved, f"logger resolved to {resolved}, not the stub"
