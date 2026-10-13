#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一消息库（看板的数据底座）。
- 收：由 ingest.py 从 PyWxDump 解密后的微信 DB 批量导入。
- 发：由 wechat_ui.py 键盘发送成功后即时写入（零延迟上屏）。
- 看板 dashboard.py 只从这个库读，不关心数据从哪来。

库文件默认 wechat_monitor.db（与脚本同目录），可用环境变量 WECHAT_MONITOR_DB 覆盖。
"""
import os
import sqlite3
import time
import hashlib

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wechat_monitor.db")


def _conn(path=None):
    path = path or os.environ.get("WECHAT_MONITOR_DB", DEFAULT_DB)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def init(path=None):
    conn = _conn(path)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS conversations (
            conv_id  TEXT PRIMARY KEY,
            name    TEXT,
            last_msg TEXT DEFAULT '',
            last_ts  INTEGER DEFAULT 0,
            unread   INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS messages (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            conv_id TEXT,
            role    TEXT,           -- 'me' | 'them'
            text    TEXT,
            ts      INTEGER,
            raw     TEXT,
            msg_hash TEXT
        );
        CREATE TABLE IF NOT EXISTS sent_log (
            conv_id TEXT,
            text    TEXT,
            ts      INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(conv_id, ts);
        """
    )
    # 老库可能没有 msg_hash 列，补齐（ALTER 不支持 IF NOT EXISTS，用异常吞掉已存在）
    try:
        conn.execute("ALTER TABLE messages ADD COLUMN msg_hash TEXT")
    except sqlite3.OperationalError:
        pass
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_msg_hash ON messages(msg_hash)"
    )
    conn.commit()
    return conn


def add_message(conv_id, role, text, ts=None, raw=None, name=None, msg_key=None):
    """写入一条消息。msg_key 用于去重（如微信 MsgSvrID）；缺省则用 会话+角色+时间+内容 推算。
    返回是否真正新增（被去重跳过则返回 False）。"""
    ts = ts or int(time.time())
    if msg_key is None:
        msg_key = f"{conv_id}|{role}|{ts}|{text}"
    h = hashlib.sha256(msg_key.encode("utf-8")).hexdigest()
    conn = init()
    cur = conn.cursor()
    if name:
        cur.execute(
            "INSERT OR IGNORE INTO conversations(conv_id, name) VALUES(?, ?)",
            (conv_id, name),
        )
        cur.execute("UPDATE conversations SET name=? WHERE conv_id=?", (name, conv_id))
    cur.execute(
        "INSERT OR IGNORE INTO messages(conv_id, role, text, ts, raw, msg_hash) "
        "VALUES(?, ?, ?, ?, ?, ?)",
        (conv_id, role, text, ts, raw, h),
    )
    inserted = cur.rowcount > 0
    if inserted:
        cur.execute(
            "UPDATE conversations SET last_msg=?, last_ts=? WHERE conv_id=?",
            (text, ts, conv_id),
        )
        if role == "them":
            cur.execute(
                "UPDATE conversations SET unread=unread+1 WHERE conv_id=?",
                (conv_id,),
            )
    conn.commit()
    conn.close()
    return inserted


def bump_unread(conv_id, n=1):
    conn = init()
    conn.execute("UPDATE conversations SET unread=unread+? WHERE conv_id=?", (n, conv_id))
    conn.commit()
    conn.close()


def add_sent(conv_id, text, ts=None):
    """记录一条「程序键盘发送」的消息（conv_id+text+时间）。
    供收消息轮询去重：数据库里再读到这条自己的消息时跳过，避免双渲染。"""
    conn = init()
    conn.execute(
        "INSERT INTO sent_log(conv_id, text, ts) VALUES(?, ?, ?)",
        (conv_id, text, ts or int(time.time())),
    )
    conn.commit()
    conn.close()


def was_sent(conv_id, text, within_seconds=120):
    """最近 within_seconds 秒内程序是否给该会话发过同文本消息。"""
    cutoff = int(time.time()) - within_seconds
    conn = _conn()
    row = conn.execute(
        "SELECT 1 FROM sent_log WHERE conv_id=? AND text=? AND ts>=? LIMIT 1",
        (conv_id, text, cutoff),
    ).fetchone()
    conn.close()
    return row is not None


def clear_unread(conv_id):
    conn = init()
    conn.execute("UPDATE conversations SET unread=0 WHERE conv_id=?", (conv_id,))
    conn.commit()
    conn.close()


def list_conversations():
    conn = _conn()
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM conversations ORDER BY last_ts DESC"
    ).fetchall()]
    conn.close()
    return rows


def get_messages(conv_id, limit=300):
    conn = _conn()
    rows = [dict(r) for r in conn.execute(
        "SELECT role, text, ts FROM messages WHERE conv_id=? ORDER BY ts ASC LIMIT ?",
        (conv_id, limit),
    ).fetchall()]
    conn.close()
    return rows
