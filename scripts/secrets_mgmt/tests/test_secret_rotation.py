"""Tests for the secret_rotation.py CLI — the subcommands and what they push, write and skip.

The logic each subcommand runs on is tested beside it: test_secret_classify.py,
test_secret_registry.py, test_secret_consumers.py, test_secret_git_dates.py, and
test_rotation_tools.py for the process boundaries. What is left here is the CLI's own
behaviour — the Kuma summary line, the unattended pick-up window, the two ways
`cmd_rotate` can fail partway through a batch (a crash and a hang), and that `rotate` picks
what is due from the same git-advanced dates `audit` reads. The push-token shape check its
audit arm calls is tested beside that module, in test_secret_sops_io.py.

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

    args = SimpleNamespace(
        name=name, all=False, commit=True, deploy=False, no_derive=False
    )
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

    args = SimpleNamespace(
        name=None, all=True, commit=True, deploy=True, no_derive=False
    )
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

    args = SimpleNamespace(
        name=None, all=True, commit=True, deploy=True, no_derive=False
    )
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


# ── `source: record` keys (issue #1914) ─────────────────────────────────────
#
# For authelia_password, healthchecks_password and bazarr_api_key, SOPS holds a copy of a
# credential the app owns: nothing in the tree writes the value to the app, so `sops set` plus
# a deploy reads green and rotates nothing. Every fixture below classifies the record key
# `auto`, so a refusal that came from the tier check instead would fail these tests: the
# record check must be the one that fires.


def _record_registry(name: str, **extra) -> dict:
    return {
        "entries": {
            name: {"tier": "auto", "last_rotated": "2026-01-01", **extra},
        }
    }


def test_rotate_refuses_a_record_key_by_name_with_the_reason(capsys):
    name = "app_owned_push_token"
    tools, recorded = build_tools(
        Fakes(registry=_record_registry(name, source="record"))
    )
    args = SimpleNamespace(
        name=name, all=False, commit=True, deploy=False, no_derive=False
    )
    assert sr.cmd_rotate(args, tools) == 2
    err = capsys.readouterr().err
    assert name in err and "source: record" in err and "Rotate it in the app" in err
    assert process_calls(recorded) == [], "a record key must never reach `sops set`"
    assert "save_registry" not in named_calls(recorded)


def test_rotate_writes_the_same_auto_key_when_it_is_not_a_record():
    """The accepting half: identical fixture minus `source: record` is rotated."""
    name = "app_owned_push_token"
    tools, recorded = build_tools(Fakes(registry=_record_registry(name)))
    args = SimpleNamespace(
        name=name, all=False, commit=True, deploy=False, no_derive=False
    )
    assert sr.cmd_rotate(args, tools) == 0
    assert len(process_calls(recorded)) == 1


def test_unattended_rotate_skips_a_record_key_and_says_so(capsys):
    """The weekly cron's path has no `--name`, so the batch filter is the only guard there."""
    name = "monitor_bridge_test_token"  # a name consumer_tags() resolves, so only the record
    tools, recorded = build_tools(  # field stands between it and `sops set`
        Fakes(registry=_record_registry(name, source="record"))
    )
    args = SimpleNamespace(
        name=None, all=True, commit=True, deploy=False, no_derive=False
    )
    assert sr.cmd_rotate(args, tools) == 0
    assert process_calls(recorded) == []
    assert (
        "skip (record: the app holds the source) %s" % name in capsys.readouterr().out
    )


def test_unattended_rotate_still_writes_the_same_key_without_the_record_field():
    name = "monitor_bridge_test_token"
    tools, recorded = build_tools(Fakes(registry=_record_registry(name)))
    args = SimpleNamespace(
        name=None, all=True, commit=True, deploy=False, no_derive=False
    )
    assert sr.cmd_rotate(args, tools) == 0
    assert len(process_calls(recorded)) == 1


# ── rotate judges due-ness from git-advanced dates, as audit does (issue #2020) ─────────
#
# `sync` leaves an existing `last_rotated` alone, so a token rotated by hand and committed
# without its date moved reads as overdue from the registry alone. `audit` closes that gap by
# advancing the date to git's; until #2020, `rotate` selected from the raw dates and would have
# rotated such a token a second time. Both names carry the `monitor_bridge_` prefix so that
# `consumer_tags` resolves them and the batch filter is not what keeps one out of the run.
HAND_ROTATED = "monitor_bridge_hand_rotated_token"
STALE = "monitor_bridge_stale_token"


def _two_token_fixture() -> Fakes:
    # Both recorded 2026-01-01: overdue at TODAY (2026-09-01) under the 180-day auto tier.
    # Git shows HAND_ROTATED's ciphertext changed on 2026-08-01 — a hand rotation nobody
    # dated — while STALE's has not changed since the oldest revision, so git agrees with
    # the registry there and the entry is genuinely overdue.
    return Fakes(
        registry=_reg(
            (HAND_ROTATED, "auto", "2026-01-01"), (STALE, "auto", "2026-01-01")
        ),
        history=[
            ("c", "2026-08-01", {HAND_ROTATED: "ENC[new]", STALE: "ENC[same]"}),
            ("b", "2026-05-01", {HAND_ROTATED: "ENC[old]", STALE: "ENC[same]"}),
            ("a", "2026-01-01", {HAND_ROTATED: "ENC[old]", STALE: "ENC[same]"}),
        ],
    )


def test_rotate_skips_the_entry_git_shows_rotated_and_still_takes_the_overdue_one(
    capsys,
):
    tools, _recorded = build_tools(_two_token_fixture())
    args = SimpleNamespace(
        name=None, all=False, commit=False, deploy=False, no_derive=False
    )
    assert sr.cmd_rotate(args, tools) == 0
    out = capsys.readouterr().out
    assert "DRY-RUN would rotate %-40s" % STALE in out, out
    assert "DRY-RUN would rotate %-40s" % HAND_ROTATED not in out, out
    assert "date advanced: %-30s 2026-01-01 -> 2026-08-01" % HAND_ROTATED in out


def test_rotate_no_derive_trusts_the_recorded_date(capsys):
    """The flag's own red proof: the same fixture with the derive off selects both."""
    tools, _recorded = build_tools(_two_token_fixture())
    args = SimpleNamespace(
        name=None, all=False, commit=False, deploy=False, no_derive=True
    )
    assert sr.cmd_rotate(args, tools) == 0
    out = capsys.readouterr().out
    assert "DRY-RUN would rotate %-40s" % HAND_ROTATED in out, out
    assert "DRY-RUN would rotate %-40s" % STALE in out, out
    assert "date advanced" not in out


def test_rotate_commit_writes_back_only_the_dates_it_rotated():
    """The derive lands on a copy: git stays the source of truth for what this run skips.

    `audit` never saves the registry, so its in-memory advance is invisible on disk.
    `rotate --commit` does save it, and the weekly cron commits what it writes — so an
    advance applied to the saved registry would bake git's dates into
    `secret_rotation.yml` for secrets the run never touched.
    """
    tools, recorded = build_tools(_two_token_fixture())
    args = SimpleNamespace(
        name=None, all=False, commit=True, deploy=False, no_derive=False
    )
    assert sr.cmd_rotate(args, tools) == 0
    saved = [c[1][0] for c in recorded if c[0] == "save_registry"]
    assert len(saved) == 1
    assert saved[0]["entries"][STALE]["last_rotated"] == "2026-09-01"  # rotated today
    assert saved[0]["entries"][HAND_ROTATED]["last_rotated"] == "2026-01-01", (
        "the git-derived date must not reach the registry on disk"
    )
