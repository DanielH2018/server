"""Remove the settings this role owns from healthchecks' `/config/local_settings.py`.

`hc/settings.py` ends with `if (BASE_DIR / "hc/local_settings.py").exists(): from
.local_settings import *`, and that import runs AFTER every environment variable is read. So
any name that file assigns wins over the Deployment's env and the role's Secret, silently.

This instance's copy was written by the image on first run in January 2023 and has assigned
EMAIL_HOST, EMAIL_PORT, EMAIL_HOST_USER, EMAIL_HOST_PASSWORD, EMAIL_USE_TLS and
DEFAULT_FROM_EMAIL ever since. Every email change the role made was therefore inert: the
Secret's `EMAIL_HOST_PASSWORD` was never read, and the Deployment's `EMAIL_USE_SSL` collided
with the file's `EMAIL_USE_TLS = True` to raise `EMAIL_USE_TLS/EMAIL_USE_SSL are mutually
exclusive` at send time.

`SECRET_KEY` joined that list on 2026-09-10 (#1491). The image generated it into this file on
first boot in 2023 and it lived nowhere else — not in SOPS, not in the rotation registry — so
nothing this repo runs could rotate it, and a lost PVC lost the key with it. The Secret now
renders a FRESH `healthchecks_secret_key` and this script deletes the file's assignment, which
costs one round of logged-out sessions and broken password-reset links. Preserving the 2023
value instead would have meant reading a live credential out of a pod and into a transcript.

**The env var is what stops the image minting a replacement.** `init-healthchecks-config/run`
appends a random `SECRET_KEY` to this file only when `${SECRET_KEY}` is EMPTY *and* the file
has no `^SECRET_KEY`. With the Secret rendering it, the first test fails and the image leaves
the file alone — so the strip is a genuine one-shot. Were the env var ever dropped while this
name stayed in OWNED, the image would rewrite the assignment on every boot and this script
would delete it again, reporting `changed` and rolling the pod forever.

The lines are DELETED rather than commented out. The file has held a plaintext Gmail
credential on the PVC since 2023; a commented-out assignment leaves it exactly where it is.

Everything else the file assigns is left byte-for-byte alone — `SITE_NAME`, `SITE_ROOT` and
`CSRF_TRUSTED_ORIGINS` among them, the last of which the image's init reads back out of this
file when the env var is unset. Removing the owned assignments is the smallest edit that makes
the role's own settings authoritative; rewriting the file wholesale is not.

Piped into the pod's `python3` by `tasks/main.yml`, which appends the `main()` call the way
it does for `seed_discord_channel.py`. There is deliberately NO `if __name__ == "__main__"`
guard: on stdin `__name__` IS `"__main__"`, so a guard plus the appended call runs main()
TWICE. The first pass strips the file and reports its count, the second finds nothing and
reports 0, and `changed_when` reads the LAST marker — so the work happens and the task
reports `ok`, skipping the restart that makes the work take effect. That shipped once, in
PR #1492. The task prints nothing else, so the
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
    "SECRET_KEY",
)

# Anchored at column 0 with `=` following, so a continuation line, an indented assignment inside
# a function, or a mention in a comment is not matched. EMAIL_HOST must not eat EMAIL_HOST_USER,
# which is why the name is followed by `\s*=` rather than by a word boundary alone. The column-0
# anchor is what keeps `SECRET_KEY` off `S3_SECRET_KEY`, a real `hc/settings.py` name the role
# does not own.
_ASSIGNMENT = re.compile(r"^(%s)\s*=" % "|".join(OWNED))


def strip_owned_settings(text: str) -> tuple[str, int]:
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

    stripped, removed = strip_owned_settings(original)
    if removed:
        # Rewritten in place rather than through a temp file and rename: the image's own init
        # owns the inode's mode and ownership, and a rename would hand it this process's umask.
        with open(PATH, "w", encoding="utf-8") as fh:
            fh.write(stripped)
    print("LOCAL_SETTINGS_CHANGED: %d" % removed)
