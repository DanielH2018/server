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


def _stub_gh(tmp_path: Path) -> None:
    """A `gh` that reports PR 939 already merged, so land.sh exits early and annotates."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        '  *"--json state,title"*) printf "MERGED\\tAlready merged\\n" ;;\n'
        '  *) printf "\\n" ;;\n'
        "esac\n"
    )
    gh.chmod(0o755)


def test_land_annotation_is_intercepted(tmp_path, logger_calls):
    """The stub caught the landing line, so nothing reached the host's syslog.

    This is the non-vacuity half: `logger_calls` being empty would mean either that land.sh
    never annotated (so the fixture proves nothing) or that the real `logger` took the call.
    Both are failures.
    """
    _stub_gh(tmp_path)
    env = {
        **os.environ,
        "PATH": f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}",
        "LAND_PRIMARY": str(tmp_path),
    }
    subprocess.run(
        ["bash", str(_LAND_SH), "--pr", "939", "--arm-merge"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    captured = logger_calls.read_text()
    assert captured.strip(), (
        "the stubbed logger recorded nothing — either land.sh no longer annotates, or the "
        "real /usr/bin/logger took the call and this run reached syslog"
    )
    assert "event=landing" in captured
    assert "pr=939" in captured


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
