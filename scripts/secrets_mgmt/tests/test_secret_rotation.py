"""Tests for the secret_rotation.py CLI — the subcommands and what they push, write and skip.

The logic each subcommand runs on is tested beside it: test_secret_classify.py,
test_secret_registry.py, test_secret_consumers.py, test_secret_git_dates.py, and
test_rotation_tools.py for the process boundaries. What is left here is the CLI's own
behaviour — the Kuma summary line, the unattended pick-up window, and the two ways
`cmd_rotate` can fail partway through a batch (a crash and a hang). The push-token shape check its audit arm calls
is tested beside that module, in test_secret_sops_io.py.

Run: uv run pytest scripts/secrets_mgmt/tests/test_secret_rotation.py
"""

import datetime as dt
import subprocess
from dataclasses import replace
from types import SimpleNamespace

from secrets_mgmt import secret_rotation as sr
from _rotation_fakes import Fakes, build_tools, named_calls, process_calls
from secrets_mgmt.secret_registry import audit
from secrets_mgmt.rotation_tools import SECRETS_FILE


def _reg(*entries):
    return {
        "entries": {
            name: {"tier": tier, "last_rotated": lr} for name, tier, lr in entries
        }
    }


# ── the Kuma summary line ───────────────────────────────────────────────────
def test_audit_summary_names_overdue_secrets():
    # The pushed Kuma msg must NAME which secret is overdue — a bare count can't tell a genuine
    # cron break from one of the consumer-less known-manual auto tokens merely coming due (M1).
    today = dt.date(2026, 6, 11)
    reg = _reg(
        ("secret_rotation_push_token", "auto", "2025-01-01"),
        ("fresh_push_token", "auto", "2026-06-01"),
    )
    summary = sr.audit_summary(audit(reg, today), [], [])
    assert "secret_rotation_push_token" in summary
    assert "1 auto" in summary


def test_audit_summary_clean_when_nothing_overdue():
    today = dt.date(2026, 6, 11)
    reg = _reg(("fresh_push_token", "auto", "2026-06-01"))
    assert (
        sr.audit_summary(audit(reg, today), [], [])
        == "all secrets within rotation window"
    )


def test_audit_summary_caps_the_overdue_name_list():
    today = dt.date(2026, 6, 11)
    reg = _reg(*[("t%02d_push_token" % i, "auto", "2025-01-01") for i in range(8)])
    summary = sr.audit_summary(audit(reg, today), [], [])
    assert "+3 more" in summary  # 8 overdue → first 5 named, then "+3 more"


# ── the unattended pick-up window ───────────────────────────────────────────
def test_unattended_rotation_picks_tokens_up_before_they_go_overdue():
    # Weekly cron + rotate-only-when-overdue left every token overdue up to 6 days while
    # the daily audit paged DOWN on it (2026-07-09 review). The pick-up window must catch
    # anything due within the next cron interval, and still catch a genuinely missed one.
    rows = [
        ("due_next_week_push_token", "auto", dt.date(2026, 7, 14), 5),
        ("missed_push_token", "auto", dt.date(2026, 7, 6), -3),
        ("not_due_push_token", "auto", dt.date(2026, 9, 7), 60),
        ("app_password", "assisted", dt.date(2026, 7, 10), 1),  # never auto-rotated
    ]
    names = [r[0] for r in sr.unattended_due(rows)]
    assert "due_next_week_push_token" in names  # rotates BEFORE going overdue
    assert "missed_push_token" in names  # a missed rotation still gets caught
    assert "not_due_push_token" not in names  # staggering preserved
    assert "app_password" not in names
    assert len(sr.unattended_due(rows, rotate_all=True)) == 3  # --all: every auto row


def test_unattended_rotation_lead_exceeds_the_cron_interval():
    # The lead window must be longer than the weekly cron interval, else a token due the
    # day after a Sunday run goes overdue before the next run — the exact gap this fixes.
    assert sr.ROTATE_LEAD_DAYS > 7


# ── cmd_rotate: the new token must not reach sops via argv ──────────────────
def test_rotate_commit_sends_new_token_on_stdin_not_argv():
    """Regression guard for the 2026-08-27 fix: the new token travels on stdin, not argv.

    `sops set` used to take the freshly minted token as a CLI argument, which sits in
    /proc/<pid>/cmdline for the call's lifetime (no hidepid here — see secret-rotate.sh.j2's own
    argv-avoidance comment for curl). The value must travel on stdin, and --value-stdin still
    requires the JSON-quoted form.
    """
    name = "monitor_bridge_test_token"
    # A real overdue auto-tier row, so the REAL `audit` selects it — the row the rotation
    # acts on is then the one the tool would compute, not one a fake asserted into place.
    tools, recorded = build_tools(
        Fakes(
            registry={"entries": {name: {"tier": "auto", "last_rotated": "2026-01-01"}}}
        )
    )

    args = SimpleNamespace(name=name, all=False, commit=True, deploy=False)
    assert sr.cmd_rotate(args, tools) == 0
    assert "save_registry" in named_calls(recorded), (
        "the new date was never written back"
    )

    calls = process_calls(recorded)
    assert len(calls) == 1
    cmd, kwargs = calls[0]
    assert cmd == [
        "sops",
        "set",
        "--value-stdin",
        SECRETS_FILE,
        '["%s"]' % name,
    ], (
        "sops set must take only the file and index positionally — no value argument, "
        "which is where the token used to leak into argv: %s" % cmd
    )
    assert kwargs.get("input", "").startswith('"') and kwargs["input"].endswith('"'), (
        "the value sent on stdin must stay JSON-quoted — sops set --value-stdin rejects a "
        "raw (unquoted) string with 'Value for --set is not valid JSON'"
    )


def test_rotate_records_the_names_a_failed_batch_already_wrote():
    """A `sops set` that fails partway must leave the registry agreeing with secrets.yml.

    Each `sops set` writes the encrypted store on its own, so when the second one fails the
    first secret's NEW value is already in the file while its `last_rotated` has only been
    updated in memory. Letting the exception escape left those two disagreeing, with nothing
    saying which secrets had moved — and the rotation is the one operation where a silent
    half-state means a live credential nobody can date.

    The failure must also not deploy: the cron reverts both tracked files on a non-zero exit,
    so deploying tokens that are about to be reverted out of the tree would leave the cluster
    running values the repo no longer records.
    """
    first, second = "monitor_bridge_alpha_push_token", "monitor_bridge_beta_push_token"
    reg = _reg((first, "auto", "2025-01-01"), (second, "auto", "2025-06-01"))
    tools, recorded = build_tools(Fakes(registry=reg))

    attempted: list[str] = []

    def failing_sops_set(name: str, _value: str) -> None:
        attempted.append(name)
        if len(attempted) == 2:
            raise subprocess.CalledProcessError(1, ["sops", "set", "--value-stdin"])

    # `RotationTools` is frozen, so a per-test boundary is a `replace`, never a setattr.
    tools = replace(tools, sops_set=failing_sops_set)

    args = SimpleNamespace(name=None, all=True, commit=True, deploy=True)
    assert sr.cmd_rotate(args, tools) == 3
    # Most-overdue first, so the failure lands on the second of two — the half that proves
    # earlier names were already written. A runner that failed the FIRST call would leave that
    # untested.
    assert attempted == [first, second]
    assert "save_registry" in named_calls(recorded), (
        "the already-written secret's new date was never recorded"
    )
    assert (
        reg["entries"][first]["last_rotated"] == "2026-09-01"
    )  # _rotation_fakes.TODAY
    assert reg["entries"][second]["last_rotated"] == "2025-06-01"  # never written
    assert "run" not in named_calls(recorded), (
        "a failed batch must not deploy — the tokens it wrote are about to be reverted"
    )


def test_rotate_reports_the_names_already_written_when_a_sops_set_hangs(capsys):
    """A hung `sops set` is the same half-state as a crashed one, and reports the same way.

    `sops_set` bounds its write at 30s, so a hang arrives at the batch as a
    `subprocess.TimeoutExpired` rather than as a call that never returns. That is a
    different exception class from the crash path, so leaving it out of the handler's
    except tuple would let it escape past the `save_registry` — the exact half-state the
    handler exists to record: NEW values in the store for the earlier names, with nothing
    saying which.
    """
    first, second = "monitor_bridge_alpha_push_token", "monitor_bridge_beta_push_token"
    reg = _reg((first, "auto", "2025-01-01"), (second, "auto", "2025-06-01"))
    tools, recorded = build_tools(Fakes(registry=reg))

    attempted: list[str] = []

    def hanging_sops_set(name: str, _value: str) -> None:
        attempted.append(name)
        if len(attempted) == 2:
            raise subprocess.TimeoutExpired(["sops", "set", "--value-stdin"], 30)

    tools = replace(tools, sops_set=hanging_sops_set)

    args = SimpleNamespace(name=None, all=True, commit=True, deploy=True)
    assert sr.cmd_rotate(args, tools) == 3
    assert attempted == [first, second]

    err = capsys.readouterr().err
    already = err.split("Already written to secrets.yml: ")[1].split(".")[0]
    assert already == first, (
        "the exit-3 message must still list the names already written: %r" % already
    )
    assert "save_registry" in named_calls(recorded)
    assert (
        reg["entries"][first]["last_rotated"] == "2026-09-01"
    )  # _rotation_fakes.TODAY
    assert reg["entries"][second]["last_rotated"] == "2025-06-01"  # never written
    assert "run" not in named_calls(recorded), (
        "a timed-out batch must not deploy, for the same reason a failed one must not"
    )
