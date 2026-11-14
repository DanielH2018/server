import datetime as dt

from lib.exit_codes import RECOVERY_KIT_BAD_ARGS, RECOVERY_KIT_OK, RECOVERY_KIT_STALE
from secrets_mgmt.recovery_kit import MAX_AGE_DAYS, main, read_refreshed

TODAY = dt.date(2026, 11, 14)


def _check(stamp, capsys) -> tuple[int, str]:
    rc = main(["check"], stamp=stamp, today=TODAY)
    return rc, capsys.readouterr().out


def test_a_kit_refreshed_within_the_limit_is_clean(tmp_path, capsys):
    stamp = tmp_path / "recovery_kit.yml"
    refreshed = TODAY - dt.timedelta(days=MAX_AGE_DAYS)
    stamp.write_text(f"refreshed: '{refreshed}'\n")

    rc, out = _check(stamp, capsys)

    assert rc == RECOVERY_KIT_OK
    assert f"{MAX_AGE_DAYS} days ago" in out


def test_a_kit_past_the_limit_is_flagged(tmp_path, capsys):
    stamp = tmp_path / "recovery_kit.yml"
    refreshed = TODAY - dt.timedelta(days=MAX_AGE_DAYS + 1)
    stamp.write_text(f"refreshed: '{refreshed}'\n")

    rc, out = _check(stamp, capsys)

    assert rc == RECOVERY_KIT_STALE
    assert f"last refreshed {refreshed}, {MAX_AGE_DAYS + 1} days ago" in out
    assert "recovery_kit.py record" in out


def test_a_missing_stamp_is_flagged(tmp_path, capsys):
    rc, out = _check(tmp_path / "recovery_kit.yml", capsys)

    assert rc == RECOVERY_KIT_STALE
    assert "no off-site recovery kit refresh is recorded" in out


def test_a_stamp_without_a_date_is_flagged(tmp_path, capsys):
    stamp = tmp_path / "recovery_kit.yml"
    stamp.write_text("refreshed: soon\n")

    rc, out = _check(stamp, capsys)

    assert rc == RECOVERY_KIT_STALE
    assert "no off-site recovery kit refresh is recorded" in out


def test_a_stamp_dated_after_today_is_flagged(tmp_path, capsys):
    stamp = tmp_path / "recovery_kit.yml"
    stamp.write_text(f"refreshed: '{TODAY + dt.timedelta(days=1)}'\n")

    rc, out = _check(stamp, capsys)

    assert rc == RECOVERY_KIT_STALE
    assert "after today" in out


def test_record_writes_a_stamp_check_reads_as_fresh(tmp_path, capsys):
    stamp = tmp_path / "recovery_kit.yml"

    assert main(["record"], stamp=stamp, today=TODAY) == RECOVERY_KIT_OK
    assert read_refreshed(stamp) == TODAY
    assert _check(stamp, capsys)[0] == RECOVERY_KIT_OK


def test_record_takes_an_earlier_date(tmp_path):
    stamp = tmp_path / "recovery_kit.yml"

    main(["record", "--date", "2026-10-01"], stamp=stamp, today=TODAY)

    assert read_refreshed(stamp) == dt.date(2026, 10, 1)


def test_record_refuses_a_date_after_today(tmp_path):
    stamp = tmp_path / "recovery_kit.yml"

    rc = main(["record", "--date", "2026-11-15"], stamp=stamp, today=TODAY)

    assert rc == RECOVERY_KIT_BAD_ARGS
    assert not stamp.exists()
