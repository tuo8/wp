"""
Unified Database Adapter (SQLite WAL + PostgreSQL)
==================================================
Lets MyDrive run zero-config on SQLite (single-node) and transparently switch
to PostgreSQL when `DATABASE_URL=postgresql://...` is set in multi-instance
cluster deployments — with automatic SQL dialect translation:
- `?` placeholders -> `%s` (ignoring string literals)
- `INSERT OR IGNORE INTO ...` -> `INSERT INTO ... ON CONFLICT DO NOTHING`
- `INTEGER PRIMARY KEY AUTOINCREMENT` -> `BIGSERIAL PRIMARY KEY`
- `BLOB` -> `BYTEA`
- `DualRow`: supports both positional `row[0]` and dict-style `row["col"]`
"""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence


def translate_sql_to_pg(sql: str) -> str:
    """Translate standard SQLite DDL/DML syntax used in MyDrive into PostgreSQL."""
    out = sql
    # 1. DDL normalizations
    out = re.sub(r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b", "BIGSERIAL PRIMARY KEY", out, flags=re.I)
    # SQLite INTEGER is signed 64-bit; PostgreSQL INTEGER is only 32-bit.
    # Use BIGINT for all remaining INTEGER columns to preserve quotas, sizes, and millisecond timestamps.
    out = re.sub(r"\bINTEGER\b", "BIGINT", out, flags=re.I)
    out = re.sub(r"\bBLOB\b", "BYTEA", out, flags=re.I)

    # 2. INSERT OR IGNORE -> INSERT ... ON CONFLICT DO NOTHING
    if re.search(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", out, flags=re.I):
        out = re.sub(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", "INSERT INTO", out, flags=re.I)
        if "ON CONFLICT" not in out.upper():
            out = out.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"

    # 3. Replace `?` placeholders outside single-quoted string literals with `%s`
    res = []
    in_single = False
    i = 0
    n = len(out)
    while i < n:
        ch = out[i]
        if ch == "'":
            res.append(ch)
            if in_single and i + 1 < n and out[i + 1] == "'":
                res.append("'")
                i += 2
                continue
            in_single = not in_single
        elif ch == "?" and not in_single:
            res.append("%s")
        else:
            res.append(ch)
        i += 1
    return "".join(res)


class DualRow:
    """Row wrapper supporting both `row[0]` indexing and `row['column']` lookup."""

    def __init__(self, columns: Sequence[str], values: Sequence[Any]):
        self._cols = list(columns)
        self._vals = tuple(values)
        self._map = dict(zip(self._cols, self._vals))

    def __getitem__(self, key):
        if isinstance(key, (int, slice)):
            return self._vals[key]
        return self._map[key]

    def __iter__(self):
        return iter(self._vals)

    def __len__(self):
        return len(self._vals)

    def keys(self):
        return self._cols

    def get(self, key: str, default=None):
        return self._map.get(key, default)


class PgCursorWrapper:
    """Wraps a psycopg2/psycopg cursor with SQLite-compatible API."""

    def __init__(self, cur):
        self._cur = cur

    @property
    def description(self):
        return self._cur.description

    @property
    def rowcount(self):
        return self._cur.rowcount

    @property
    def lastrowid(self):
        return getattr(self._cur, "lastrowid", None)

    def execute(self, sql: str, params: Optional[Iterable] = None):
        pg_sql = translate_sql_to_pg(sql)
        pragma = re.match(r"\s*PRAGMA\s+table_info\s*\(\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?\s*\)\s*;?\s*$", sql, flags=re.I)
        if pragma:
            table_name = pragma.group(1)
            self._cur.execute(
                """SELECT (c.ordinal_position - 1)::int AS cid,
                          c.column_name AS name,
                          c.data_type AS type,
                          CASE WHEN c.is_nullable = 'NO' THEN 1 ELSE 0 END AS not_null,
                          c.column_default AS dflt_value,
                          CASE WHEN EXISTS (
                              SELECT 1
                              FROM information_schema.table_constraints tc
                              JOIN information_schema.key_column_usage kcu
                                ON tc.constraint_name = kcu.constraint_name
                               AND tc.table_schema = kcu.table_schema
                               AND tc.table_name = kcu.table_name
                              WHERE tc.constraint_type = 'PRIMARY KEY'
                                AND tc.table_schema = c.table_schema
                                AND tc.table_name = c.table_name
                                AND kcu.column_name = c.column_name
                          ) THEN 1 ELSE 0 END AS pk
                   FROM information_schema.columns c
                   WHERE c.table_schema = current_schema() AND c.table_name = %s
                   ORDER BY c.ordinal_position""",
                (table_name,),
            )
            return self
        if pg_sql.strip().upper().startswith("PRAGMA"):
            return self
        if params is None:
            self._cur.execute(pg_sql)
        else:
            self._cur.execute(pg_sql, tuple(params))
        return self

    def executemany(self, sql: str, seq_of_params):
        pg_sql = translate_sql_to_pg(sql)
        self._cur.executemany(pg_sql, [tuple(p) for p in seq_of_params])
        return self

    def _wrap(self, row):
        if row is None:
            return None
        if self._cur.description:
            cols = [d[0] for d in self._cur.description]
            return DualRow(cols, row)
        return row

    def fetchone(self):
        return self._wrap(self._cur.fetchone())

    def __iter__(self):
        for row in self._cur:
            yield self._wrap(row)

    def fetchall(self):
        rows = self._cur.fetchall()
        if not rows or not self._cur.description:
            return rows
        cols = [d[0] for d in self._cur.description]
        return [DualRow(cols, r) for r in rows]


class PgConnectionWrapper:
    """Wraps a PostgreSQL connection to behave like sqlite3.Connection."""

    def __init__(self, conn):
        self._conn = conn
        self.row_factory = None

    def cursor(self) -> PgCursorWrapper:
        return PgCursorWrapper(self._conn.cursor())

    def execute(self, sql: str, params: Optional[Iterable] = None) -> PgCursorWrapper:
        cur = self.cursor()
        cur.execute(sql, params)
        return cur

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            self.rollback()
        else:
            self.commit()
        self.close()
        return False


def is_postgres_enabled(pg_url: Optional[str] = None) -> bool:
    url = (pg_url if pg_url is not None else os.environ.get("DATABASE_URL", "")).strip()
    return url.startswith(("postgres://", "postgresql://"))


def connect_db(sqlite_path: Path | str, timeout: float = 5.0, pg_url: Optional[str] = None):
    """Connect to PostgreSQL if DATABASE_URL is configured, otherwise SQLite."""
    url = (pg_url if pg_url is not None else os.environ.get("DATABASE_URL", "")).strip()
    if url.startswith(("postgres://", "postgresql://")):
        try:
            import psycopg2  # type: ignore
            return PgConnectionWrapper(psycopg2.connect(url, connect_timeout=int(max(2, timeout))))
        except ImportError:
            try:
                import psycopg  # type: ignore
                return PgConnectionWrapper(psycopg.connect(url, connect_timeout=int(max(2, timeout))))
            except ImportError as exc:
                raise RuntimeError(
                    "DATABASE_URL is set to PostgreSQL, but neither psycopg2 nor psycopg is installed. "
                    "Install via: pip install psycopg2-binary"
                ) from exc
    return sqlite3.connect(str(sqlite_path), timeout=timeout)
