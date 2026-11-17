"""SQLite 连接与 schema 管理。

设计要点：
- 每个请求使用独立连接，事务边界即请求边界；
- 写请求用 BEGIN IMMEDIATE，读请求用普通 BEGIN，WAL 模式下读事务持有快照，
  保证"上传与查询交错时，层级、读数和结果取同一已提交状态"；
- 任何校验失败都发生在写入之前或事务回滚，已提交的旧数据不受影响。
"""
from __future__ import annotations

import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS hierarchy (
    child_meter  TEXT PRIMARY KEY,   -- 一只表至多一个上级
    parent_meter TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS readings (
    meter_id   TEXT NOT NULL,
    ts_utc     TEXT NOT NULL,        -- 规范化为 'YYYY-MM-DDTHH:00:00Z'
    kwh        REAL NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (meter_id, ts_utc)
);

CREATE TABLE IF NOT EXISTS batches (
    batch_id   TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,        -- 'hierarchy' | 'readings'
    status     TEXT NOT NULL,        -- 'applied' | 'rejected'
    reason     TEXT,
    row_count  INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
"""


def connect(db_path: str) -> sqlite3.Connection:
    """新建连接。isolation_level=None 表示自动提交，事务由代码显式控制。"""
    conn = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def init_db(db_path: str) -> None:
    directory = os.path.dirname(os.path.abspath(db_path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        # WAL：读写可并发，读事务看到一致的已提交快照
        conn.execute("PRAGMA journal_mode = WAL")
    finally:
        conn.close()
