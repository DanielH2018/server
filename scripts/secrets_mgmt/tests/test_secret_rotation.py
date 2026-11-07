"""Tests for the secret_rotation.py CLI — the subcommands and what they push, write and skip.

The logic each subcommand runs on is tested beside it: test_secret_classify.py,
test_secret_registry.py, test_secret_consumers.py, test_secret_git_dates.py, and
test_rotation_tools.py for the process boundaries. What is left here is the CLI's own
behaviour — the audit's summary line and verdict, its refusal of a tier with no cadence, and
`record`.

Run: uv run pytest scripts/secrets_mgmt/tests/test_secret_rotation.py
"""

import copy
import datetime as dt
from types import SimpleNamespace

from lib import yaml_fast

from secrets_mgmt import secret_rotation as sr
from _rotation_fakes import Fakes, build_tools, named_calls
from secrets_mgmt.secrets_lib.secret_registry import audit


def _reg(*entries):
    return {
        "entries": {
            name: {"tier": tier, "last_rotated": lr} for name, tier, lr in entries
        }
    }


# ── the audit's summary line ────────────────────────────────────────────────
def test_audit_summary_names_overdue_secrets():
    # The summary line must NAME which secret is overdue, so the journal says what to rotate.
    today = dt.date(2026, 6, 11)
    reg = _reg(
        ("secret_rotation_push_token", "assisted", "2025-01-01"),
        ("fresh_push_token", "assisted", "2026-06-01"),
    )
    summary = sr.audit_summary(audit(reg, today), [], [])
    assert "secret_rotation_push_token" in summary
    assert "1 assisted" in summary


def test_audit_summary_clean_when_nothing_overdue():
    today = dt.date(2026, 6, 11)
    reg = _reg(("fresh_push_token", "assisted", "2026-06-01"))
    assert (
        sr.audit_summary(audit(reg, today), [], [])
        == "all secrets within rotation window"
    )


def _audit_verdict_args():
    return SimpleNamespace(
        verdict=True, check=False, no_derive=True, extra_down=None, all=False
    )


def test_audit_verdict_exits_nonzero_on_a_down_audit(capsys):
    # The daily audit runs under a kuma-check timer whose Restart=on-failure reruns it while
    # red, and its wrapper writes the exit code as the textfile verdict, so a `down` audit has
    # to reach both as a non-zero exit.
    tools, _ = build_tools(
        Fakes(
            registry=_reg(("stale_push_token", "assisted", "2025-01-01")),
            names=["stale_push_token"],
        )
    )
    assert sr.cmd_audit(_audit_verdict_args(), tools) == 1
    assert "audit: verdict=down 1 secret(s) overdue" in capsys.readouterr().out


def test_audit_verdict_exits_zero_on_an_up_audit(capsys):
    tools, _ = build_tools(
        Fakes(
            registry=_reg(("fresh_push_token", "assisted", "2026-09-01")),
            names=["fresh_push_token"],
        )
    )
    assert sr.cmd_audit(_audit_verdict_args(), tools) == 0
    assert (
        "audit: verdict=up all secrets within rotation window"
        in capsys.readouterr().out
    )


def test_audit_summary_caps_the_overdue_name_list():
    today = dt.date(2026, 6, 11)
    reg = _reg(*[("t%02d_push_token" % i, "assisted", "2025-01-01") for i in range(8)])
    summary = sr.audit_summary(audit(reg, today), [], [])
    assert "+3 more" in summary  # 8 overdue → first 5 named, then "+3 more"


# ── a tier with no cadence ──────────────────────────────────────────────────
# A hand edit can give a row a tier the table lacks, such as the retired weekly tier. Nothing
# dates such a row, so the audit must read down on it rather than skip it.
RETIRED = yaml_fast.safe_load("""
entries:
  retired_push_token: {tier: auto, last_rotated: '2026-08-01'}
  fresh_push_token: {tier: external, last_rotated: '2026-08-01'}
""")


def _audit_check_args():
    return SimpleNamespace(
        verdict=False, check=True, no_derive=True, extra_down=None, all=False
    )


def test_audit_check_refuses_a_row_whose_tier_has_no_cadence(capsys):
    tools, _ = build_tools(Fakes(registry=RETIRED, names=sorted(RETIRED["entries"])))
    assert sr.cmd_audit(_audit_check_args(), tools) == 1
    out, err = capsys.readouterr()
    assert "retired_push_token carry a tier with no cadence" in err
    assert "audit: verdict=down" in out
    assert "1 row(s) with no tier cadence: retired_push_token" in out


def test_audit_check_passes_the_same_registry_once_the_row_has_a_known_tier(capsys):
    reg = copy.deepcopy(RETIRED)
    reg["entries"]["retired_push_token"]["tier"] = "assisted"
    tools, _ = build_tools(Fakes(registry=reg, names=sorted(reg["entries"])))
    assert sr.cmd_audit(_audit_check_args(), tools) == 0
    assert "audit: verdict=up" in capsys.readouterr().out


def test_audit_verdict_reads_down_on_a_row_whose_tier_has_no_cadence():
    tools, _ = build_tools(Fakes(registry=RETIRED, names=sorted(RETIRED["entries"])))
    assert sr.cmd_audit(_audit_verdict_args(), tools) == 1


# ── record: the carry-over ─────────────────────────────────────────────────


def _record_args(key: str, last_rotated: str):
    return SimpleNamespace(key=key, last_rotated=dt.date.fromisoformat(last_rotated))


def test_record_moves_the_date_backward_and_saves(capsys):
    """The rename carry-over replaces a fresher seed with the true older date.

    `advance_last_rotated` may only clear an overdue; this must be allowed to create one,
    or a renamed credential keeps the fictional freshness `sync` seeded it with.
    """
    tools, recorded = build_tools(
        Fakes(registry=_reg(("tok", "assisted", "2026-08-01")))
    )
    assert sr.cmd_record(_record_args("tok", "2026-01-15"), tools) == 0
    saved = [c[1][0] for c in recorded if c[0] == "save_registry"]
    assert len(saved) == 1
    assert saved[0]["entries"]["tok"]["last_rotated"] == "2026-01-15"
    assert (
        "record: tok last_rotated 2026-08-01 -> 2026-01-15" in capsys.readouterr().out
    )


def test_record_moves_a_renamed_row_to_its_new_name(capsys):
    """A RENAMED_FROM target the registry lacks takes over its source's row, tier and all.

    Whether or not a `sync` already seeded the new name: the source row carries the
    operator's tier override, so it is the one that survives.
    """
    reg = _reg(("kopia_b2_key_id", "pinned", "2026-05-29"))
    reg["entries"]["kopia_b2_key_id"]["source"] = "record"
    tools, recorded = build_tools(Fakes(registry=reg))
    assert sr.cmd_record(_record_args("longhorn_b2_key_id", "2026-05-29"), tools) == 0
    saved = [c[1][0] for c in recorded if c[0] == "save_registry"][0]
    assert "kopia_b2_key_id" not in saved["entries"]
    assert saved["entries"]["longhorn_b2_key_id"] == {
        "tier": "pinned",
        "last_rotated": "2026-05-29",
        "source": "record",
    }
    assert "(row moved from kopia_b2_key_id)" in capsys.readouterr().out


def test_record_refuses_a_key_the_registry_lacks(capsys):
    tools, recorded = build_tools(
        Fakes(registry=_reg(("tok", "assisted", "2026-08-01")))
    )
    assert sr.cmd_record(_record_args("other", "2026-01-15"), tools) == 64
    assert "save_registry" not in named_calls(recorded)
    assert "other is not in the registry" in capsys.readouterr().err


def test_record_refuses_a_date_after_today(capsys):
    tools, recorded = build_tools(
        Fakes(registry=_reg(("tok", "assisted", "2026-08-01")))
    )
    assert sr.cmd_record(_record_args("tok", "2026-09-02"), tools) == 64
    assert "save_registry" not in named_calls(recorded)
    assert "after today" in capsys.readouterr().err
