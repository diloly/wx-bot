#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一消息库（看板的数据底座）。
- 收：由 wx_recv_bridge.py 轮询 wechatauto 解密库，水印增量写入。
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
            msg_hash TEXT,
            media   TEXT,           -- 媒体文件相对路径（wechat_media/ 下，图片/视频/文件）
            card    TEXT            -- 链接/文件卡片 JSON（title/descr/url/thumb/kind）
        );
        CREATE TABLE IF NOT EXISTS sent_log (
            conv_id TEXT,
            text    TEXT,
            ts      INTEGER
        );
        CREATE TABLE IF NOT EXISTS poll_cursor (
            conv_id   TEXT PRIMARY KEY,
            last_time INTEGER DEFAULT 0,
            max_seq   INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(conv_id, ts);
        """
    )
    # 老库可能没有 msg_hash / media / card 列，补齐（ALTER 不支持 IF NOT EXISTS，用异常吞掉已存在）
    for col in ("msg_hash", "media", "card"):
        try:
            conn.execute("ALTER TABLE messages ADD COLUMN %s TEXT" % col)
        except sqlite3.OperationalError:
            pass
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_msg_hash ON messages(msg_hash)"
    )
    # 一次性迁移：清理 2026-10-13 消息映射错误时期产生的坏占位记录
    # （text 形如 [消息类型XXX]，content 被错误覆盖）。
    # msg_hash 不变会被去重挡住正确文本，故删掉让新逻辑重新入库。幂等。
    cur = conn.execute("DELETE FROM messages WHERE text LIKE '[消息类型%]'")
    if cur.rowcount and cur.rowcount > 0:
        print("[monitor_store] 已清理 %d 条坏占位记录，将按正确文本重新入库" % cur.rowcount)
    conn.commit()
    return conn


def stats():
    """库存消息按角色统计，如 {'me': 12, 'them': 340}。"""
    conn = _conn()
    rows = conn.execute("SELECT role, COUNT(*) AS c FROM messages GROUP BY role").fetchall()
    conn.close()
    return {r["role"]: r["c"] for r in rows}


def add_message(conv_id, role, text, ts=None, raw=None, name=None, msg_key=None,
                media=None, card=None):
    """写入一条消息。msg_key 用于去重（如微信 MsgSvrID）；缺省则用 会话+角色+时间+内容 推算。
    media=媒体文件相对路径（wechat_media/ 下）；card=链接/文件卡片 JSON。
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
        "INSERT OR IGNORE INTO messages(conv_id, role, text, ts, raw, msg_hash, media, card) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
        (conv_id, role, text, ts, raw, h, media, card),
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


def get_cursor(conv_id):
    """取某会话的轮询水印（last_time/max_seq）；没有返回 None。"""
    conn = _conn()
    row = conn.execute(
        "SELECT last_time, max_seq FROM poll_cursor WHERE conv_id=?", (conv_id,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    return {"last_time": row["last_time"], "max_seq": row["max_seq"]}


def update_media(conv_id, msg_key, media=None, card=None, text=None):
    """按 msg_key 补更一条已存消息的媒体/卡片/文本（用于历史占位消息的媒体回填）。
    返回**真正发生变更**的行数（0=没这条记录或值本来就没变）。

    注意：sqlite3 的 cursor.rowcount 对 UPDATE 返回的是「匹配行数」而非「变更行数」，
    值完全一样也照样返回 1，会让幂等回填每次都报同样的补更条数、掩盖真实信号。
    故这里先读旧值比对，只有真有差异才执行 UPDATE。"""
    h = hashlib.sha256(msg_key.encode("utf-8")).hexdigest()
    conn = init()
    row = conn.execute(
        "SELECT media, card, text FROM messages WHERE conv_id=? AND msg_hash=?",
        (conv_id, h)).fetchone()
    if not row:
        conn.close()
        return 0
    sets, params = [], []
    if media is not None and row["media"] != media:
        sets.append("media=?")
        params.append(media)
    if card is not None and row["card"] != card:
        sets.append("card=?")
        params.append(card)
    if text is not None and row["text"] != text:
        sets.append("text=?")
        params.append(text)
    if not sets:
        conn.close()
        return 0
    params.extend([conv_id, h])
    cur = conn.execute(
        "UPDATE messages SET %s WHERE conv_id=? AND msg_hash=?" % ",".join(sets), params)
    n = cur.rowcount
    conn.commit()
    conn.close()
    return n


def set_cursor(conv_id, last_time, max_seq):
    """更新某会话的轮询水印。"""
    conn = init()
    conn.execute(
        "INSERT INTO poll_cursor(conv_id, last_time, max_seq) VALUES(?, ?, ?) "
        "ON CONFLICT(conv_id) DO UPDATE SET last_time=excluded.last_time, max_seq=excluded.max_seq",
        (conv_id, int(last_time or 0), int(max_seq or 0)),
    )
    conn.commit()
    conn.close()


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
        "SELECT role, text, ts, media, card FROM messages WHERE conv_id=? ORDER BY ts ASC LIMIT ?",
        (conv_id, limit),
    ).fetchall()]
    conn.close()
    return rows
