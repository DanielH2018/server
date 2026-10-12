"""Pure decision core for judging whether a configarr sync run succeeded.

Written for the Docker-era host cron, whose I/O shell was retired with the k3s migration; the
caller is now configarr_health_logic.py, reading the last CronJob's exit code + logs. Kept
stdlib-only (runs via `uv run --no-project --python <pin>`, host_python_version in
ansible/inventory/group_vars/all.yml), and unit-testable without a
cluster: the caller hands it an exit code plus combined output and it returns the verdict.

Why not trust the exit code alone: recyclarr's healthcheck watched only the supercronic PROCESS, so
the 2026-06-10 v8 breakage failed every nightly sync while the container looked healthy. Capturing
the exit code into a monitored state file already fixes that; scanning the output for an error-level
line is the backstop for a soft failure that still exits 0.
"""

from __future__ import annotations

import re

# An error-LEVEL line: an optional run of non-word chars (log brackets / stripped ANSI) then the
# ERROR/FATAL level token on a word boundary. Anchored at line start so a benign "0 errors" summary
# or "Checking for errors..." never trips it (a bare `"error" in output` substring would). Tune the
# token set against real configarr output in Task 9 if a clean run pages.
_ERROR_LINE = re.compile(r"(?im)^[^\w]*(?:error|fatal)\b")


def has_error_line(output) -> bool:
    return bool(_ERROR_LINE.search(output or ""))


def summarize(output, maxlen: int = 200) -> str:
    """Returns the last meaningful line of configarr's output, collapsed and length-capped.

    The useful tail for a Kuma/Discord one-liner: on a clean run that is configarr's Execution
    Summary, on a failure its error. The Job's log is configarr's output alone, so no line is
    skipped. Empty output returns a fixed placeholder.
    """
    lines = [ln.strip() for ln in (output or "").splitlines() if ln.strip()]
    if not lines:
        return "(no output)"
    tail = " ".join(lines[-1].split())
    return tail[: maxlen - 3] + "..." if len(tail) > maxlen else tail


def evaluate(returncode, output):
    """Whether a configarr sync run succeeded, as (ok, msg) for the {ts,ok,msg} state file.

    ok=False on a nonzero exit OR an error-level line in the output (a soft content failure that
    still exits 0 — the class recyclarr's process-only healthcheck missed). ok=True only on a clean
    exit with no error line.
    """
    if returncode != 0:
        return False, "configarr sync failed (exit %d): %s" % (
            returncode,
            summarize(output),
        )
    if has_error_line(output):
        return False, "configarr sync logged an error: %s" % summarize(output)
    return True, "configarr sync ok: %s" % summarize(output)
