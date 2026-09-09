"""Remove the email settings from healthchecks' `/config/local_settings.py`.

`hc/settings.py` ends with `if (BASE_DIR / "hc/local_settings.py").exists(): from
.local_settings import *`, and that import runs AFTER every environment variable is read. So
any name that file assigns wins over the Deployment's env and the role's Secret, silently.

This instance's copy was written by the image on first run in January 2023 and has assigned
EMAIL_HOST, EMAIL_PORT, EMAIL_HOST_USER, EMAIL_HOST_PASSWORD, EMAIL_USE_TLS and
DEFAULT_FROM_EMAIL ever since. Every email change the role made was therefore inert: the
Secret's `EMAIL_HOST_PASSWORD` was never read, and the Deployment's `EMAIL_USE_SSL` collided
with the file's `EMAIL_USE_TLS = True` to raise `EMAIL_USE_TLS/EMAIL_USE_SSL are mutually
exclusive` at send time.

The lines are DELETED rather than commented out. The file has held a plaintext Gmail
credential on the PVC since 2023; a commented-out assignment leaves it exactly where it is.

Everything else the file assigns is left byte-for-byte alone. `SECRET_KEY` above all: it signs
sessions and password-reset tokens, it exists nowhere else (not in SOPS, not in the rotation
registry), and rewriting the file wholesale would silently mint a new one and log everyone out.
Removing six lines is the smallest edit that makes the role's own settings authoritative.

Piped into the pod's `python3` by `tasks/main.yml`, which prints nothing else, so the
`LOCAL_SETTINGS_CHANGED:` marker on the last line is what Ansible reads for `changed`.
"""

import re

PATH = "/config/local_settings.py"

# The names the role owns through the Deployment env and its Secret. A name assigned here is a
# name Django reads from this file instead, so each one is a setting the role only appears to
# control.
OWNED = (
    "EMAIL_HOST",
    "EMAIL_PORT",
    "EMAIL_HOST_USER",
    "EMAIL_HOST_PASSWORD",
    "EMAIL_USE_TLS",
    "EMAIL_USE_SSL",
    "DEFAULT_FROM_EMAIL",
)

# Anchored at column 0 with `=` following, so a continuation line, an indented assignment inside
# a function, or a mention in a comment is not matched. EMAIL_HOST must not eat EMAIL_HOST_USER,
# which is why the name is followed by `\s*=` rather than by a word boundary alone.
_ASSIGNMENT = re.compile(r"^(%s)\s*=" % "|".join(OWNED))


def strip_email_settings(text: str) -> tuple[str, int]:
    """The file with the owned assignments removed, and how many lines went.

    Returns the input unchanged when nothing matches, so a second run is a no-op and the task
    reports `changed=false` rather than rewriting an identical file every deploy.
    """
    kept = []
    removed = 0
    for line in text.splitlines(keepends=True):
        if _ASSIGNMENT.match(line):
            removed += 1
            continue
        kept.append(line)
    return "".join(kept), removed


def main() -> None:
    try:
        with open(PATH, encoding="utf-8") as fh:
            original = fh.read()
    except FileNotFoundError:
        # No file is the end state this task exists to approximate, not an error.
        print("LOCAL_SETTINGS_CHANGED: 0")
        return

    stripped, removed = strip_email_settings(original)
    if removed:
        # Rewritten in place rather than through a temp file and rename: the image's own init
        # owns the inode's mode and ownership, and a rename would hand it this process's umask.
        with open(PATH, "w", encoding="utf-8") as fh:
            fh.write(stripped)
    print("LOCAL_SETTINGS_CHANGED: %d" % removed)


if __name__ == "__main__":
    main()
