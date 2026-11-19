"""Write one day's rows into a warehouse table, replacing whatever that day held.

Every load DAG writes through `replace_partition`, so a rerun or a backfill of any day is safe:
the day's old rows and the new ones never coexist. Works on any DB-API connection; the DAGs pass
the Postgres hook's connection.

Typical usage example:

    replace_partition(conn, "prom.cpu_temp_daily", "day", day, ["day", "host", "max_c"], rows)
"""

from collections.abc import Iterable, Sequence
from datetime import date

# The role Grafana reads as. The Postgres init script creates it with no grant on any schema;
# `ensure_schema`, run as the schema's owner, grants it each one.
READER_ROLE = "warehouse_reader"


def ensure_schema(cursor, schema: str, ddl: Iterable[str]) -> None:
    """Create a source's schema and tables if missing, and let the reader role read them.

    Args:
      cursor: A DB-API cursor on the warehouse database.
      schema: The source's schema name, such as `prom`.
      ddl: `CREATE TABLE IF NOT EXISTS` statements for the schema's tables.
    """
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
