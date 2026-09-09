"""The strip must remove exactly the email settings and nothing else.

Both halves matter, and only one of them is obvious. Removing too little leaves
`/config/local_settings.py` overriding the role's Secret, which is the bug. Removing too much
takes `SECRET_KEY` with it — it signs sessions and password-reset tokens, it exists nowhere
else in this repo, and a script that truncated the file would satisfy every "the email lines
are gone" assertion perfectly. So the survival check is not a nicety here; it is the half that
can go wrong silently.

The fixture is shaped like the real file: the six assignments the 2023 copy carries, plus the
four names it also sets, in the same flat top-level form.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "files"))

from strip_local_settings_email import strip_email_settings

SURVIVORS = (
    'SECRET_KEY = "not-the-real-one"',
    'SITE_NAME = "Home Server healthchecks"',
    'SITE_ROOT = "https://healthchecks.example.com"',
    'CSRF_TRUSTED_ORIGINS = ["https://healthchecks.example.com"]',
)

EMAIL_LINES = (
    'EMAIL_HOST = "smtp.gmail.com"',
    "EMAIL_PORT = 587",
    'EMAIL_HOST_USER = "someone@example.com"',
    'EMAIL_HOST_PASSWORD = "stale-app-password"',
    "EMAIL_USE_TLS = True",
    'DEFAULT_FROM_EMAIL = "someone@example.com"',
)

REAL_SHAPED = "\n".join(SURVIVORS + EMAIL_LINES) + "\n"


def test_every_email_assignment_is_removed():
    stripped, removed = strip_email_settings(REAL_SHAPED)
    assert removed == len(EMAIL_LINES)
    for line in EMAIL_LINES:
        assert line not in stripped


def test_every_other_assignment_survives_byte_for_byte():
    """The half a truncating script would also pass without."""
    stripped, _ = strip_email_settings(REAL_SHAPED)
    for line in SURVIVORS:
        assert line in stripped
    assert stripped == "\n".join(SURVIVORS) + "\n"


def test_the_plaintext_credential_is_gone_not_commented():
    """A commented-out assignment leaves the 2023 Gmail password on the PVC."""
    stripped, _ = strip_email_settings(REAL_SHAPED)
    assert "stale-app-password" not in stripped


def test_a_second_run_reports_no_change():
    """Idempotence, which is also what stops the task rolling the pod every deploy."""
    once, first = strip_email_settings(REAL_SHAPED)
    twice, second = strip_email_settings(once)
    assert first > 0
    assert second == 0
    assert twice == once


def test_a_file_with_no_email_settings_is_untouched():
    text = "\n".join(SURVIVORS) + "\n"
    stripped, removed = strip_email_settings(text)
    assert removed == 0
    assert stripped == text


def test_email_host_does_not_swallow_email_host_user():
    """`EMAIL_HOST` is a prefix of `EMAIL_HOST_USER`, so the match anchors on the `=`."""
    stripped, removed = strip_email_settings('EMAIL_HOST_USER = "a@b.c"\n')
    assert removed == 1
    assert stripped == ""


def test_an_indented_or_quoted_mention_is_left_alone():
    """Only a top-level assignment shadows the environment; a mention inside a body does not."""
    text = '# EMAIL_HOST = "old"\ndef f():\n    EMAIL_PORT = 25\n'
    stripped, removed = strip_email_settings(text)
    assert removed == 0
    assert stripped == text


def test_email_use_ssl_is_removed_too():
    """The role owns both switches, so the file must not pin either one."""
    _stripped, removed = strip_email_settings("EMAIL_USE_SSL = False\n")
    assert removed == 1
