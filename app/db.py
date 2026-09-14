"""SQLite 持久层（标准库 sqlite3，每次请求一个连接）。

快照表 snapshots 通过触发器禁止 UPDATE/DELETE，保证复原快照不可变。
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    start_date  TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'draft',   -- draft / solved / rejected
    last_error  TEXT,                            -- 最近一次整案拒绝的错误明细（JSON）
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS medications (
    case_id TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    seq     INTEGER NOT NULL,
    med_id  TEXT NOT NULL,
    payload TEXT NOT NULL,                       -- 建案时药品录入的原始 JSON
    PRIMARY KEY (case_id, seq)
);

CREATE TABLE IF NOT EXISTS observations (
    case_id    TEXT PRIMARY KEY REFERENCES cases(id) ON DELETE CASCADE,
    payload    TEXT NOT NULL,                    -- 残留 + 散落观察的原始 JSON
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    id         TEXT PRIMARY KEY,
    case_id    TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    version    INTEGER NOT NULL,
    input_hash TEXT NOT NULL,                    -- sha256:... 输入规范化 JSON 的哈希
    input_json TEXT NOT NULL,                    -- 生成该快照时的完整输入（不可变留存）
    result_json TEXT NOT NULL,                   -- 复原结果
    created_at TEXT NOT NULL,
    UNIQUE (case_id, version)
);

-- 不可变约束：快照只允许插入，不允许修改或删除
CREATE TRIGGER IF NOT EXISTS snapshots_no_update
BEFORE UPDATE ON snapshots
BEGIN
    SELECT RAISE(ABORT, 'snapshots are immutable');
END;

CREATE TRIGGER IF NOT EXISTS snapshots_no_delete
BEFORE DELETE ON snapshots
BEGIN
    SELECT RAISE(ABORT, 'snapshots are immutable');
END;
"""


def db_path() -> str:
    return os.environ.get("PILLBOX_DB_PATH", "data/pillbox.db")


def connect() -> sqlite3.Connection:
    path = db_path()
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def get_db():
    """FastAPI 依赖：每请求一个连接。"""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()
