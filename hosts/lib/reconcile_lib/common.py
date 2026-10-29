"""The path root, the logger and the command runners every reconcile module shares."""

import os
import subprocess
import sys
from pathlib import Path

# HOMELAB_ROOT prefixes every path reconcile reads or writes. Only the tests set it.
ROOT = Path(os.environ.get("HOMELAB_ROOT", "/"))


# sd-daemon(3) priorities. Without one, journald files a line at the unit's default priority,
# so `journalctl -p err` finds no failure (#4446, #6202).
ERR = 3
WARNING = 4


def _is_journal(stream) -> bool:
    """Whether `stream` is the journal stream systemd connected, per `JOURNAL_STREAM`."""
    try:
        dev, ino = os.environ["JOURNAL_STREAM"].split(":")
        st = os.fstat(stream.fileno())
        return (st.st_dev, st.st_ino) == (int(dev), int(ino))
    except (KeyError, ValueError, OSError, AttributeError):
        return False


def log(
    message: str,
    level: int | None = None,
    *,
    tag: str = "homelab-reconcile",
    stream=None,
) -> None:
    """Print `<tag>: <message>` to stderr, each line prefixed `<level>` on the journal.

    The distro-python tier's copy of host_lib's `log`: reconcile and homelab-secrets run
    under the host's /usr/bin/python3 and import nothing from /opt. It writes the same
    prefix rule, so a terminal or a test never sees `<3>`. Every host package ships the
    reconcile component, so homelab-secrets imports this module from /usr/lib/homelab too.
    """
    out = sys.stderr if stream is None else stream
    text = f"{tag}: {message}"
    if level is not None and _is_journal(out):
        text = "\n".join(f"<{level}>{line}" for line in text.split("\n"))
    print(text, file=out, flush=True)


def under_root(path: Path) -> Path:
    return ROOT / path.relative_to("/")


def run(*argv: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, input=stdin, capture_output=True, text=True)
    except OSError as exc:
        return subprocess.CompletedProcess(argv, 127, "", str(exc))


def systemctl(*args: str) -> subprocess.CompletedProcess:
    return run("systemctl", *args)
