"""No systemd unit template in the repo may interpolate a secret into ExecStart.

systemd serves unit content over the system bus, so `systemctl show <unit> -p ExecStart`
prints the rendered command to any local user regardless of the file's mode. A tree walk
rather than an enumeration, so a new unit cannot inherit the shape unseen; alert units must
also read a dedicated webhook file, since the role's config.env is exactly what can be
unreadable when the thing they alert for has failed. Alert units also retry a failed delivery.
"""

# ansible/roles/setup/gitops_deploy/tests/test_systemd_unit_secrets.py

import pathlib
import re

_ROLES = pathlib.Path(__file__).parents[3]

# A Jinja interpolation naming a secret. Matched on the VARIABLE NAME, not on "does this
# ExecStart interpolate anything" — the broad form flags six legitimate units that interpolate
# a path or a username (claude-rc.service.j2, both gitops-deploy units, both renovate-notify
# units, one retired archive/ template). Only the secret-named form isolates a real leak.
_SECRET_VAR = re.compile(
    r"\{\{[^}]*\b\w+(?:_webhook|_token|_password|_secret|_key)\b[^}]*\}\}"
)


def _unit_templates() -> list[pathlib.Path]:
    """Every systemd unit template in the repo, minus retired code.

    A TREE WALK, not an enumeration. A guard that names its paths can only prove the units its
    own fix touched, so it derives its own corpus.
    """
    return sorted(_ROLES.rglob("*.service.j2"))


def test_no_unit_template_embeds_a_secret_in_execstart():
    # A unit that interpolates the SOPS webhook straight into ExecStart cannot rely on
    # `mode: 0600` to protect it: systemd serves unit content over the system bus, so
    # `systemctl show <unit> -p ExecStart` prints the full webhook URL to any local user with
    # no sudo. With EnvironmentFile the same command prints the literal ${ALERT_WEBHOOK}.
    #
    # A comment claiming the protection is the least reliable evidence in the file, so the claim
    # gets a test — and the test derives which files it covers rather than being told.
    units = _unit_templates()
    assert units, (
        f"No *.service.j2 found under {_ROLES} — the walk is broken, not the tree."
    )
    for unit_path in units:
        unit = unit_path.read_text()
        exec_start = re.search(
            r"^ExecStart=.*?(?=\n(?!\s)|\Z)", unit, re.MULTILINE | re.DOTALL
        )
        if not exec_start:
            continue  # a .timer-adjacent or Type=oneshot-less unit; nothing to leak
        leaked = _SECRET_VAR.search(exec_start.group(0))
        assert not leaked, (
            f"{unit_path.relative_to(_ROLES)} interpolates {leaked.group(0)} into ExecStart. "
            f"`systemctl show` will print it to any local user regardless of the unit file's "
            f'mode. Reference it as "${{ALERT_WEBHOOK}}" and supply it with EnvironmentFile= '
            f"instead."
        )


def test_alert_units_read_a_dedicated_webhook_file():
    # The other half of the same contract: an alert unit must not fall back to its role's
    # config.env. That file is exactly what can be unreadable when the thing this unit alerts
    # for has failed, which would leave the alert unable to page.
    alerts = [p for p in _unit_templates() if p.name.endswith("-alert.service.j2")]
    assert alerts, "No *-alert.service.j2 found — the walk is broken, not the tree."
    for unit_path in alerts:
        unit = unit_path.read_text()
        assert re.search(
            r"^EnvironmentFile=\S*alert-webhook\.env$", unit, re.MULTILINE
        ), (
            f"{unit_path.relative_to(_ROLES)} does not read a dedicated alert-webhook.env. It "
            f"must NOT fall back to the role's config.env — that file is exactly what can be "
            f"unreadable when the thing this unit alerts for has failed."
        )


def _retries_every_failure(exec_start: str) -> bool:
    """Whether an alert's curl retries a refused connection and a failed lookup.

    Measured with curl 8.5: plain `--retry` retried a DNS failure (exit 6) but not a refused
    connection (exit 7); `--retry-all-errors` retried both.
    """
    return (
        bool(re.search(r"--retry [1-9]", exec_start))
        and "--retry-all-errors" in exec_start
    )


def test_the_retry_rule_rejects_a_single_attempt_and_plain_retry():
    retrying = (
        'ExecStart=/usr/bin/curl -fsS --retry 5 --retry-all-errors "${ALERT_WEBHOOK}"'
    )
    assert _retries_every_failure(retrying)
    assert not _retries_every_failure(
        'ExecStart=/usr/bin/curl -fsS -m 10 "${ALERT_WEBHOOK}"'
    )
    assert not _retries_every_failure(
        'ExecStart=/usr/bin/curl -fsS --retry 5 "${ALERT_WEBHOOK}"'
    )


def test_alert_units_retry_a_failed_delivery():
    # One attempt lost the page for claude-rc's 2026-10-04 07:46 crash: curl exited 6 (could
    # not resolve host) at the moment the host failed, and nothing tried again (#3523).
    alerts = {
        p.name: p for p in _unit_templates() if p.name.endswith("-alert.service.j2")
    }
    assert "claude-rc-alert.service.j2" in alerts, (
        "the walk no longer finds the #3523 unit"
    )
    for unit_path in alerts.values():
        exec_start = re.search(
            r"^ExecStart=.*?(?=\n(?!\s)|\Z)",
            unit_path.read_text(),
            re.MULTILINE | re.DOTALL,
        )
        assert exec_start and _retries_every_failure(exec_start.group(0)), (
            f"{unit_path.relative_to(_ROLES)} sends its page once. Give its curl "
            f"`--retry 5 --retry-delay 10 --retry-all-errors`, or, if it no longer uses curl, "
            f"an equivalent retry that covers a refused connection and a failed DNS lookup."
        )
