"""`replace_partition` against sqlite, which shares the DB-API transaction semantics it relies on."""

import sqlite3
from datetime import date

import pytest

from dags.homelab_warehouse.warehouse import replace_partition

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
