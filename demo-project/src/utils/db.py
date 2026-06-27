"""
Database utility — 数据库工具

模拟数据库连接（实际项目使用 SQLAlchemy / sqlite3）
"""

import sqlite3
import threading
from contextlib import contextmanager

_local = threading.local()


def get_db():
    """获取当前线程的数据库连接"""
    if not hasattr(_local, "db") or _local.db is None:
        _local.db = sqlite3.connect("app.db")
        _local.db.row_factory = sqlite3.Row
    return _DatabaseWrapper(_local.db)


class _DatabaseWrapper:
    """数据库操作包装器"""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def query(self, sql: str, params: tuple = ()) -> dict | None:
        """执行查询，返回单行"""
        cursor = self._conn.execute(sql, params)
        row = cursor.fetchone()
        return dict(row) if row else None

    def query_all(self, sql: str, params: tuple = ()) -> list[dict]:
        """执行查询，返回多行"""
        cursor = self._conn.execute(sql, params)
        return [dict(row) for row in cursor.fetchall()]

    def execute(self, sql: str, params: tuple = ()) -> int:
        """执行写操作"""
        cursor = self._conn.execute(sql, params)
        return cursor.rowcount

    def insert(self, sql: str, params: tuple = ()) -> int:
        """执行插入并返回自增 ID"""
        cursor = self._conn.execute(sql, params)
        return cursor.lastrowid

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()
