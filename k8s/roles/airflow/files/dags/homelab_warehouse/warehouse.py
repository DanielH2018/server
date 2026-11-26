"""Write one day's rows into a warehouse table, replacing whatever that day held, and read them back.

Every load DAG writes through `replace_partition`, so a rerun or a backfill of any day is safe:
the day's old rows and the new ones never coexist. Its check task reads through
`read_partition`. Both work on any DB-API connection and use no driver-specific cursor; the
DAGs pass the Postgres hook's connection, which is psycopg 3 or psycopg2 by what is installed.

Typical usage example:

    replace_partition(conn, "prom.cpu_temp_daily", "day", day, ["day", "host", "max_c"], rows)
"""

from collections.abc import Callable, Iterable, Sequence
from datetime import date, datetime, timezone

# The init script creates this role with no grants; `ensure_schema` grants it each schema.
READER_ROLE = "warehouse_reader"
# Concurrent GRANTs on one catalog row fail ("tuple concurrently updated"), so every load's DDL
# takes this lock.
DDL_LOCK_KEY = 74_657_201

# One row per DAG and day whose check passed, which the freshness DAG reads.
CHECK_SCHEMA = "meta"
CHECK_TABLE = f"{CHECK_SCHEMA}.check_log"
CHECK_DDL = (
    f"CREATE TABLE IF NOT EXISTS {CHECK_TABLE} (dag_id text NOT NULL, day date NOT NULL, "
    "checked_at timestamptz NOT NULL, PRIMARY KEY (dag_id, day))",
)


def ensure_schema(cursor, schema: str, ddl: Iterable[str]) -> None:
    """Create a source's schema and tables if missing, and let the reader role read them.

    Holds a transaction-scoped advisory lock until the caller commits, so concurrent loads
    run their DDL one at a time.

    Args:
      cursor: A DB-API cursor on the warehouse database, inside a transaction.
      schema: The source's schema name, such as `prom`.
      ddl: `CREATE TABLE IF NOT EXISTS` statements for the schema's tables.
    """
    cursor.execute(f"SELECT pg_advisory_xact_lock({DDL_LOCK_KEY})")
    cursor.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
    for statement in ddl:
        cursor.execute(statement)
    cursor.execute(f"GRANT USAGE ON SCHEMA {schema} TO {READER_ROLE}")
    cursor.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA {schema} TO {READER_ROLE}")


def replace_partition(
    conn,
    table: str,
    day_column: str,
    day: date,
    columns: Sequence[str],
    rows: Iterable[Sequence],
    placeholder: str = "%s",
) -> int:
    """Delete `table`'s rows for `day`, then insert `rows`, in one transaction.

    Args:
      conn: A DB-API connection. It is committed on success and rolled back on any error.
      table: The table, schema-qualified where the database has schemas.
      day_column: The column holding each row's day.
      day: The partition being replaced.
      columns: The column names, in the order each row's values come in.
      rows: The day's rows. Each must carry `day` in its `day_column` position.
      placeholder: The driver's parameter marker; `%s` for psycopg2.

    Returns:
      The number of rows inserted.

    Raises:
      ValueError: A row's day is not `day`, which would put it in another partition and
        survive that partition's next replace.
    """
    rows = [tuple(row) for row in rows]
    day_index = list(columns).index(day_column)
    for row in rows:
        if row[day_index] != day:
            raise ValueError(
                f"{table}: a row for {row[day_index]} in the {day} partition"
            )
    insert = (
        f"INSERT INTO {table} ({', '.join(columns)}) "
        f"VALUES ({', '.join([placeholder] * len(columns))})"
    )
    cursor = conn.cursor()
    try:
        cursor.execute(
            f"DELETE FROM {table} WHERE {day_column} = {placeholder}", (day,)
        )
        if rows:
            cursor.executemany(insert, rows)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
    return len(rows)


def read_partition(
    conn, table: str, day_column: str, day: date, placeholder: str = "%s"
) -> list[dict]:
    """`table`'s rows for `day`, each a dict keyed by column name.

    Args:
      conn: A DB-API connection.
      table: The table, schema-qualified where the database has schemas.
      day_column: The column holding each row's day.
      day: The partition read.
      placeholder: The driver's parameter marker; `%s` for psycopg.
    """
    cursor = conn.cursor()
    try:
        cursor.execute(
            f"SELECT * FROM {table} WHERE {day_column} = {placeholder}", (day,)
        )
        names = [column[0] for column in cursor.description]
        return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]
    finally:
        cursor.close()


def record_check(
    conn, dag_id: str, day: date, checked_at: datetime, placeholder: str = "%s"
) -> None:
    """Record that `dag_id`'s check passed for `day`, replacing any earlier record of it."""
    cursor = conn.cursor()
    try:
        cursor.execute(
            f"DELETE FROM {CHECK_TABLE} WHERE dag_id = {placeholder} AND day = {placeholder}",
            (dag_id, day),
        )
        cursor.execute(
            f"INSERT INTO {CHECK_TABLE} (dag_id, day, checked_at) "
            f"VALUES ({placeholder}, {placeholder}, {placeholder})",
            (dag_id, day, checked_at),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()


def finish_check(
    connect: Callable, dag_id: str, day: date, problems: Sequence[str]
) -> None:
    """End a check task: fail it on any problem, or record that the day passed.

    The record is what `warehouse_freshness` reads, so a check that passes without calling
    this reads as a missing day.

    Args:
      connect: Opens a DB-API connection to the warehouse, such as a Postgres hook's
        `get_conn`. It is called only when the day passed.
      dag_id: The checked DAG.
      day: The checked day.
      problems: The check's findings; empty when the day is trustworthy.

    Raises:
      ValueError: `problems` is not empty.
    """
    if problems:
        raise ValueError("quality check failed:\n" + "\n".join(problems))
    conn = connect()
    try:
        cursor = conn.cursor()
        try:
            ensure_schema(cursor, CHECK_SCHEMA, CHECK_DDL)
        finally:
            cursor.close()
        conn.commit()
        record_check(conn, dag_id, day, datetime.now(timezone.utc))
    finally:
        conn.close()
