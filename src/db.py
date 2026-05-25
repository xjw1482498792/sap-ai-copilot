"""
SQLite 数据访问层。

职责：
  1. init_schema()  根据 schemas.py 创建所有 SAP 表
  2. get_conn()     返回数据库连接
  3. run_sql()      执行查询并返回结构化结果（列名 + 行）
"""
import sqlite3
from contextlib import contextmanager
from typing import Any

from src.config import SQLITE_PATH
from src.schemas import SCHEMAS, schema_to_ddl


def get_conn() -> sqlite3.Connection:
    SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(SQLITE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_schema() -> None:
    """根据 SCHEMAS 创建所有表（IF NOT EXISTS，重复运行安全）。"""
    conn = get_conn()
    cur = conn.cursor()
    for table in SCHEMAS:
        cur.execute(schema_to_ddl(table))
    conn.commit()
    conn.close()


class SqlResult(dict):
    """执行结果：成功时 {ok, columns, rows, row_count}；失败时 {ok=False, error}。"""


def run_sql(sql: str, params: tuple = ()) -> SqlResult:
    """
    执行 SELECT，返回结构化结果。
    出错时 ok=False，把数据库错误信息原样返回 —— Day 6 SQL 自修复 Agent 会用这条信息。
    """
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall()
        columns = [d[0] for d in cur.description] if cur.description else []
        conn.close()
        return SqlResult(
            ok=True,
            columns=columns,
            rows=[dict(r) for r in rows],
            row_count=len(rows),
        )
    except sqlite3.Error as e:
        return SqlResult(ok=False, error=str(e), sql=sql)


def table_row_counts() -> dict[str, int]:
    """返回每张表的行数 —— 用于验证灌数据是否成功。"""
    conn = get_conn()
    cur = conn.cursor()
    counts = {}
    for t in SCHEMAS:
        try:
            cur.execute(f'SELECT COUNT(*) FROM "{t["name"]}"')
            counts[t["name"]] = cur.fetchone()[0]
        except sqlite3.Error:
            counts[t["name"]] = -1
    conn.close()
    return counts
