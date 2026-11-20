"""`replace_partition` and `read_partition` against sqlite, which shares the DB-API semantics they rely on."""

import sqlite3
from datetime import date, datetime, timezone

import pytest

from dags.homelab_warehouse.warehouse import (
    CHECK_DDL,
    finish_check,
    read_partition,
    record_check,
    replace_partition,
)

COLUMNS = ("day", "host", "max_c")
NOV_17 = date(2026, 11, 17)
NOV_18 = date(2026, 11, 18)


@pytest.fixture
def conn():
    sqlite3.register_adapter(date, date.isoformat)
    sqlite3.register_converter("date", lambda raw: date.fromisoformat(raw.decode()))
    db = sqlite3.connect(":memory:", detect_types=sqlite3.PARSE_DECLTYPES)
    db.execute(
        "CREATE TABLE temps (day date NOT NULL, host text NOT NULL, max_c real NOT NULL, PRIMARY KEY (day, host))"
    )
    yield db
    db.close()


def rows(conn) -> list[tuple]:
    return conn.execute(
        "SELECT day, host, max_c FROM temps ORDER BY day, host"
    ).fetchall()


def replace(conn, day, day_rows):
    return replace_partition(
        conn, "temps", "day", day, COLUMNS, day_rows, placeholder="?"
    )


def test_rerunning_a_day_replaces_its_rows_and_leaves_other_days_alone(conn):
    replace(
        conn, NOV_17, [(NOV_17, "daniel-box", 70.0), (NOV_17, "daniel-server", 61.0)]
    )
    replace(conn, NOV_18, [(NOV_18, "daniel-box", 72.0)])

    inserted = replace(conn, NOV_17, [(NOV_17, "daniel-box", 71.5)])

    assert inserted == 1
    assert rows(conn) == [(NOV_17, "daniel-box", 71.5), (NOV_18, "daniel-box", 72.0)]


def test_a_failed_insert_keeps_the_day_it_was_replacing(conn):
    replace(conn, NOV_17, [(NOV_17, "daniel-box", 70.0)])

    with pytest.raises(sqlite3.IntegrityError):
        replace(
            conn, NOV_17, [(NOV_17, "daniel-box", 1.0), (NOV_17, "daniel-box", 2.0)]
        )

    assert rows(conn) == [(NOV_17, "daniel-box", 70.0)]


def test_a_row_for_another_day_is_refused_before_anything_is_written(conn):
    replace(conn, NOV_17, [(NOV_17, "daniel-box", 70.0)])

    with pytest.raises(
        ValueError, match="a row for 2026-11-18 in the 2026-11-17 partition"
    ):
        replace(conn, NOV_17, [(NOV_18, "daniel-box", 72.0)])

    assert rows(conn) == [(NOV_17, "daniel-box", 70.0)]


def test_a_day_reads_back_as_one_dict_per_row_keyed_by_column(conn):
    replace(conn, NOV_17, [(NOV_17, "daniel-box", 70.0)])
    replace(conn, NOV_18, [(NOV_18, "daniel-box", 72.0)])

    assert read_partition(conn, "temps", "day", NOV_17, placeholder="?") == [
        {"day": NOV_17, "host": "daniel-box", "max_c": 70.0}
    ]


def test_a_failed_check_raises_before_it_connects():
    def connect():
        raise AssertionError("a failed check must not record a pass")

    with pytest.raises(ValueError, match="quality check failed:\nmissing host"):
        finish_check(connect, "prom_daily_rollup", NOV_17, ["missing host"])


def test_a_recheck_replaces_the_days_record(conn):
    sqlite3.register_adapter(datetime, datetime.isoformat)
    conn.execute("ATTACH DATABASE ':memory:' AS meta")
    conn.execute(CHECK_DDL[0].replace("timestamptz", "text"))
    first = datetime(2026, 11, 18, 1, tzinfo=timezone.utc)

    record_check(conn, "prom_daily_rollup", NOV_17, first, placeholder="?")
    record_check(
        conn, "prom_daily_rollup", NOV_17, first.replace(hour=2), placeholder="?"
    )

    assert conn.execute("SELECT dag_id, day FROM meta.check_log").fetchall() == [
        ("prom_daily_rollup", NOV_17)
    ]
