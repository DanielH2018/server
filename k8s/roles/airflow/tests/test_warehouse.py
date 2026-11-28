"""`replace_partition` and `read_partition` against sqlite, which shares the DB-API semantics they rely on."""

import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

from dags.homelab_warehouse.warehouse import (
    CHECK_DDL,
    check_day,
    interval_day,
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


class Borrowed:
    """The fixture's connection, which `check_day` may close and open again."""

    def __init__(self, db):
        self.db = db
        self.opened = 0

    def __call__(self):
        self.opened += 1
        return self

    def __getattr__(self, name):
        return getattr(self.db, name)

    def cursor(self):
        return WithoutPostgresOnly(self.db.cursor())

    def close(self):
        pass


class WithoutPostgresOnly:
    """A SQLite cursor that skips the statements of `ensure_schema` SQLite has no form of."""

    SKIPPED = ("SELECT pg_advisory_xact_lock", "CREATE SCHEMA", "GRANT ")

    def __init__(self, cursor):
        self.cursor = cursor

    def __getattr__(self, name):
        return getattr(self.cursor, name)

    def execute(self, sql, *params):
        if not sql.startswith(self.SKIPPED):
            self.cursor.execute(sql, *params)


def test_a_check_hands_each_tables_day_to_the_judge_and_fails_on_its_problems(conn):
    conn.execute("CREATE TABLE fans (day date NOT NULL, rpm integer NOT NULL)")
    conn.execute("INSERT INTO temps VALUES (?, 'daniel-box', 70.0)", (NOV_17,))
    conn.execute("INSERT INTO temps VALUES (?, 'daniel-box', 72.0)", (NOV_18,))
    conn.execute("INSERT INTO fans VALUES (?, 900)", (NOV_17,))
    connect = Borrowed(conn)
    judged = []

    def problems_of(day, temps, fans):
        judged.append((day, temps, fans))
        return ["missing host"]

    with pytest.raises(ValueError, match="quality check failed:\nmissing host"):
        check_day(
            connect,
            "prom_daily_rollup",
            NOV_17,
            ["temps", "fans"],
            problems_of,
            placeholder="?",
        )

    assert judged == [
        (
            NOV_17,
            [{"day": NOV_17, "host": "daniel-box", "max_c": 70.0}],
            [{"day": NOV_17, "rpm": 900}],
        )
    ]
    assert connect.opened == 1


def test_a_check_with_no_problems_records_the_dag_and_day_as_passed(conn):
    sqlite3.register_adapter(datetime, datetime.isoformat)
    conn.execute("ATTACH DATABASE ':memory:' AS meta")
    connect = Borrowed(conn)

    check_day(
        connect,
        "prom_daily_rollup",
        NOV_17,
        ["temps"],
        lambda day, temps: [],
        placeholder="?",
    )

    assert conn.execute("SELECT dag_id, day FROM meta.check_log").fetchall() == [
        ("prom_daily_rollup", NOV_17)
    ]
    assert connect.opened == 2


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


def test_a_run_loads_the_utc_day_its_interval_starts_on():
    los_angeles = timezone(timedelta(hours=-8))

    assert interval_day(datetime(2026, 11, 17, 17, 0, tzinfo=los_angeles)) == NOV_18


def test_a_task_run_with_no_data_interval_is_refused():
    with pytest.raises(ValueError, match="no data interval"):
        interval_day(None)
