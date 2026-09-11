"""The strip must remove exactly the settings the role owns and nothing else.

Both halves matter, and only one of them is obvious. Removing too little leaves
`/config/local_settings.py` overriding the role's Secret, which is the bug. Removing too much
takes `SITE_ROOT` or `CSRF_TRUSTED_ORIGINS` with it — the image's own init reads both back out
of this file when their env vars are unset, and halts on a missing `SITE_ROOT`. A script that
truncated the file would satisfy every "the owned lines are gone" assertion perfectly, so the
survival check is not a nicety here; it is the half that can go wrong silently.

`SECRET_KEY` moved from survivor to casualty on 2026-09-10 (#1491), when it gained a SOPS key
and a Secret entry to be read from. Its sibling `S3_SECRET_KEY` is a real `hc/settings.py`
name the role does NOT own, and is the prefix collision the column-0 anchor exists to survive.

The fixture is shaped like the real file: the assignments the 2023 copy carries, plus the
names it also sets, in the same flat top-level form.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "files"))

from strip_local_settings_owned import strip_owned_settings

SURVIVORS = (
    'SITE_NAME = "Home Server healthchecks"',
    'SITE_ROOT = "https://healthchecks.example.com"',
    'CSRF_TRUSTED_ORIGINS = ["https://healthchecks.example.com"]',
)

OWNED_LINES = (
    'EMAIL_HOST = "smtp.gmail.com"',
    "EMAIL_PORT = 587",
    'EMAIL_HOST_USER = "someone@example.com"',
    'EMAIL_HOST_PASSWORD = "stale-app-password"',
    "EMAIL_USE_TLS = True",
    'DEFAULT_FROM_EMAIL = "someone@example.com"',
    'SECRET_KEY = "the-2023-key"',
)

REAL_SHAPED = "\n".join(SURVIVORS + OWNED_LINES) + "\n"


def test_every_owned_assignment_is_removed():
    stripped, removed = strip_owned_settings(REAL_SHAPED)
    assert removed == len(OWNED_LINES)
    for line in OWNED_LINES:
        assert line not in stripped


def test_every_other_assignment_survives_byte_for_byte():
    """The half a truncating script would also pass without."""
    stripped, _ = strip_owned_settings(REAL_SHAPED)
    for line in SURVIVORS:
        assert line in stripped
    assert stripped == "\n".join(SURVIVORS) + "\n"


def test_the_plaintext_credential_is_gone_not_commented():
    """A commented-out assignment leaves the 2023 Gmail password on the PVC."""
    stripped, _ = strip_owned_settings(REAL_SHAPED)
    assert "stale-app-password" not in stripped


def test_the_pvc_secret_key_is_removed_so_the_secret_supplies_it():
    """The role renders `healthchecks_secret_key`; the file's copy would shadow it."""
    stripped, removed = strip_owned_settings('SECRET_KEY = "the-2023-key"\n')
    assert removed == 1
    assert stripped == ""


def test_s3_secret_key_is_not_swallowed_by_secret_key():
    """`SECRET_KEY` is a suffix of `S3_SECRET_KEY`, which the role does not own."""
    text = 'S3_SECRET_KEY = "not-ours"\n'
    stripped, removed = strip_owned_settings(text)
    assert removed == 0
    assert stripped == text


def test_a_second_run_reports_no_change():
    """Idempotence, which is also what stops the task rolling the pod every deploy."""
    once, first = strip_owned_settings(REAL_SHAPED)
    twice, second = strip_owned_settings(once)
    assert first > 0
    assert second == 0
    assert twice == once


def test_a_file_with_no_owned_settings_is_untouched():
    text = "\n".join(SURVIVORS) + "\n"
    stripped, removed = strip_owned_settings(text)
    assert removed == 0
    assert stripped == text


def test_email_host_does_not_swallow_email_host_user():
    """`EMAIL_HOST` is a prefix of `EMAIL_HOST_USER`, so the match anchors on the `=`."""
    stripped, removed = strip_owned_settings('EMAIL_HOST_USER = "a@b.c"\n')
    assert removed == 1
    assert stripped == ""


def test_an_indented_or_quoted_mention_is_left_alone():
    """Only a top-level assignment shadows the environment; a mention inside a body does not."""
    text = '# EMAIL_HOST = "old"\ndef f():\n    EMAIL_PORT = 25\n'
    stripped, removed = strip_owned_settings(text)
    assert removed == 0
    assert stripped == text


def test_email_use_ssl_is_removed_too():
    """The role owns both switches, so the file must not pin either one."""
    _stripped, removed = strip_owned_settings("EMAIL_USE_SSL = False\n")
    assert removed == 1


# --- the convention that made this script's first deploy a silent no-op -----------

FILES = Path(__file__).resolve().parents[1] / "files"
TASKS = (Path(__file__).resolve().parents[1] / "tasks" / "main.yml").read_text()

# Anchored at column 0 as a statement, not a substring: both scripts DESCRIBE the guard in
# their docstrings, and a plain `in` check fires on the prose warning against it.
GUARD = re.compile(r"^if __name__ == .__main__.:", re.M)


def scripts_the_tasks_call_main_on():
    """Every `files/*.py` that `tasks/main.yml` pipes into a pod and then calls `main()` on.

    Derived from the task file rather than listed, so a third script added the same way is
    covered without editing this test. The census is asserted non-empty below, because a glob
    that matches nothing makes the rule pass over an empty set.
    """
    return [
        p for p in sorted(FILES.glob("*.py")) if p.name in TASKS and "main()" in TASKS
    ]


def test_the_census_finds_both_known_scripts():
    names = {p.name for p in scripts_the_tasks_call_main_on()}
    assert {"seed_discord_channel.py", "strip_local_settings_owned.py"} <= names, names


def test_no_script_carries_a_main_guard():
    """A guard plus the appended `main()` runs the script twice, because this is stdin.

    `python3` reading a script from stdin sets `__name__` to `"__main__"`, so the guard fires
    on its own and the appended call fires again. For an idempotent script the second pass
    reports zero changes, `changed_when` reads the LAST marker, and the task reports `ok`
    having done the work — skipping whatever the role gated on `changed`. That is how the
    first deploy of this script stripped the file and never restarted the pod.
    """
    for path in scripts_the_tasks_call_main_on():
        assert not GUARD.search(path.read_text()), (
            f"{path.name} carries an `if __name__` guard while tasks/main.yml also appends "
            f"main(); on stdin that runs it twice and the task under-reports `changed`"
        )
