#!/usr/bin/env python3
"""The qbittorrent-prefs-check CronJob's entry point: a log-only drift check.

Runs apply_prefs.py's main() with `--dry-run --connect-wait 60` against the WebUI Service and
prints one verdict line, `qbittorrent-prefs-check: <message>`, to stdout. The pod log is the
evidence; `probe.py loki-query '{container="qbittorrent-prefs-check"}'` reads it. It replaced
the daniel-box host cron `qbittorrent prefs drift check` at H2.5d
(docs/target-architecture/host/h2-shrink-host-plane.md, Steps item 15).

It never writes to qBittorrent and pushes no Kuma monitor: a low-urgency drift check does not
earn a push token. Applying a changed DESIRED value stays a human decision, made by hand.

The exit status follows apply_prefs.py's contract. Drift (EXIT_DRIFT, 3) is a result, not a
failure, so it exits 0 as a match does. An unreachable WebUI, a rejected login, a missing
credential or any other exit leaves the Job Failed, which k8s/cronjob-gate reports without
failing the deploy. Every outcome still prints its tagged line, as the host script's `2>&1`
captured every outcome, so a rejected login or a read timeout is never an untagged traceback.

Environment:
    QBT_CREDENTIALS  the WebUI username, a newline, then the password (the
                     qbittorrent-prefs-check Secret's one key).
    QBT_URL          the WebUI base URL, read by apply_prefs.py.

Run: python3 prefs_check.py (no arguments), from a directory holding apply_prefs.py.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import sys
from collections.abc import Callable, Mapping

import apply_prefs

TAG = "qbittorrent-prefs-check"
CONNECT_WAIT_S = 60

# apply_prefs exit -> (verdict prefix, this process's exit). The prefixes are the host
# script's logger messages, so a search written for those lines still matches.
_VERDICTS = {
    apply_prefs.EXIT_OK: ("ok: preferences match", 0),
    apply_prefs.EXIT_DRIFT: ("DRIFT", 0),
    apply_prefs.EXIT_UNREACHABLE: ("cannot reach qbittorrent", 1),
    apply_prefs.EXIT_BAD_ARGS: ("credentials missing from QBT_CREDENTIALS", 2),
}


def split_credentials(value: str) -> tuple[str, str] | None:
    """Returns (username, password) from the Secret's two-line value, or None if malformed."""
    lines = value.split("\n")
    if len(lines) != 2 or not all(lines):
        return None
    return lines[0], lines[1]


def run(
    env: Mapping[str, str],
    *,
    check: Callable[[list[str]], int] = apply_prefs.main,
    emit: Callable[[str], None] = print,
) -> int:
    """Runs the dry-run check once and emits its verdict line.

    Args:
        env: The process environment; QBT_CREDENTIALS is read from it.
        check: apply_prefs.main, or a stand-in; called with the argv to run.
        emit: Writes one output line.

    Returns:
        0 for a match or drift, otherwise the non-zero code the verdict names.
    """
    creds = split_credentials(env.get("QBT_CREDENTIALS", ""))
    if creds is None:
        status, output = apply_prefs.EXIT_BAD_ARGS, ""
    else:
        os.environ["QBT_USERNAME"], os.environ["QBT_PASSWORD"] = creds
        buffer = io.StringIO()
        status: int | None = None
        failure = ""
        try:
            with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
                status = check(["--dry-run", "--connect-wait", str(CONNECT_WAIT_S)])
        # apply_prefs.QbtClient.login raises SystemExit with its message on a rejected login,
        # and urllib lets a read timeout or a dropped response escape main() unwrapped.
        except SystemExit as exc:
            failure = f"login rejected: {exc.code}"
        except Exception as exc:
            failure = f"unexpected error: {type(exc).__name__}: {exc}"
        output = " ".join(buffer.getvalue().split())
        if status is None:
            emit(f"{TAG}: {failure}: {output}" if output else f"{TAG}: {failure}")
            return 1
    message, code = _VERDICTS.get(status, (f"unexpected exit {status}", 1))
    emit(f"{TAG}: {message}: {output}" if output else f"{TAG}: {message}")
    return code


if __name__ == "__main__":
    # No options: --help prints this docstring and exits before the check runs.
    argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    ).parse_args()
    sys.exit(run(os.environ))
