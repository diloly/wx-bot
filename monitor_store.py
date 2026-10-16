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
import threading
import time
import hashlib

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wechat_monitor.db")

# SQLite 写锁等待秒数。python sqlite3 默认 5s；单线程时够用，但 2026-10-16 起
# auto_reply 有「收消息轮询 / AI 生成 / 发送」多个线程并发读写本库，撞锁概率
# 上升，放宽到 15s（再超就是真卡住，早报错反而好排查）。
_BUSY_TIMEOUT = 15


def _conn(path=None):
    path = path or os.environ.get("WECHAT_MONITOR_DB", DEFAULT_DB)
    conn = sqlite3.connect(path, timeout=_BUSY_TIMEOUT)
    conn.row_factory = sqlite3.Row
    return conn


# 默认库是否已跑过建表/迁移（init 的快速路径，见 init 的 docstring）
_init_done = False
_init_lock = threading.Lock()


def init(path=None):
    """建表 + 一次性迁移，返回连接。

    2026-10-16：默认库加**只跑一次**的快速路径。写函数（add_message 等）每次
    都会调 init()，而 init() 含 10 条 CREATE TABLE IF NOT EXISTS + 一条**全表
    扫描**的 DELETE（清理坏占位记录）+ 若干 ALTER —— 单线程时只是浪费，引入
    AI 生成 / 发送线程后，每次写都全表扫一遍会明显拉长写锁占用、放大撞锁。
    建表与迁移都是幂等的，跑过一次即可。显式传 path（测试/多库场景）时不走
    快速路径，保持原语义。
    """
    global _init_done
    if path is None and _init_done:
        return _conn(path)
    with _init_lock:
        if path is None and _init_done:
            return _conn(path)
        conn = _init_schema(path)
        if path is None:
            _init_done = True
        return conn


def _init_schema(path=None):
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
        CREATE TABLE IF NOT EXISTS conv_switch (
            conv_id    TEXT PRIMARY KEY,
            enabled    INTEGER NOT NULL DEFAULT 0,   -- 1=该会话开AI自动回复，0/无记录=关
            updated_at INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS moments (
            tid         TEXT PRIMARY KEY,             -- SnsTimeLine.tid（微信内自增）
            username    TEXT,                        -- 发布者 wxid
            nickname    TEXT,
            text        TEXT,
            create_time INTEGER,
            media       TEXT,                        -- JSON: images/videos（含 md5/宽高）
            likes       TEXT,                        -- JSON: 点赞用户列表
            comments    TEXT,                        -- JSON: 评论列表
            followed    INTEGER DEFAULT 0,           -- 1=关注客户的新圈
            seen_ts     INTEGER DEFAULT 0            -- 落库时间
        );
        CREATE TABLE IF NOT EXISTS moment_inter (
            local_id     INTEGER PRIMARY KEY,         -- SnsMessage_tmp3.local_id
            type         INTEGER,                     -- 1=赞 2=评论
            from_username TEXT,
            from_nickname TEXT,
            content      TEXT,
            feed_id      TEXT,
            create_time  INTEGER,
            unread       INTEGER DEFAULT 1,
            seen_ts      INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS moment_cursor (
            id       INTEGER PRIMARY KEY CHECK (id = 1),
            last_tid TEXT DEFAULT '0'                  -- 朋友圈轮询水印（微信tid是20位大数，超int64，必须TEXT）
        );
        CREATE TABLE IF NOT EXISTS moment_switch (
            username     TEXT PRIMARY KEY,             -- 发布者 wxid
            nickname     TEXT,                         -- 开关操作时的昵称（面板展示用，客户暂无新圈也能显示行）
            auto_like    INTEGER NOT NULL DEFAULT 0,   -- 1=该客户新圈自动点赞（默认关）
            auto_comment INTEGER NOT NULL DEFAULT 0,   -- 1=该客户新圈自动评论（不可逆，默认关）
            updated_at   INTEGER DEFAULT 0
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
    # 老库的 moment_switch 可能没有 nickname 列（2026-10-16 加，面板要展示），补齐
    try:
        conn.execute("ALTER TABLE moment_switch ADD COLUMN nickname TEXT")
    except sqlite3.OperationalError:
        pass
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_msg_hash ON messages(msg_hash)"
    )
    # 一次性迁移：moment_cursor.last_tid 早先按 INTEGER 建（微信 tid 是 20 位
    # 大数、超 int64，INTEGER 列存不下），改为 TEXT。水位丢了最多多拉一轮，
    # add_moment 按 tid 去重，无副作用。幂等。
    try:
        col = conn.execute("PRAGMA table_info(moment_cursor)").fetchall()
        types = {r[1]: (r[2] or "").upper() for r in col}
        if types.get("last_tid", "TEXT") != "TEXT":
            conn.execute("DROP TABLE moment_cursor")
            conn.execute(
                "CREATE TABLE moment_cursor ("
                "id INTEGER PRIMARY KEY CHECK (id = 1), last_tid TEXT DEFAULT '0')")
            conn.commit()
            print("[monitor_store] moment_cursor.last_tid 已迁移为 TEXT（tid 超 int64）")
    except sqlite3.OperationalError:
        pass
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


def set_switch(conv_id, enabled):
    """设置某会话的 AI 自动回复开关（看板每个会话一个，默认关）。
    upsert：不存在则插入，存在则更新。"""
    conn = init()
    conn.execute(
        "INSERT INTO conv_switch(conv_id, enabled, updated_at) VALUES(?, ?, ?) "
        "ON CONFLICT(conv_id) DO UPDATE SET enabled=excluded.enabled, updated_at=excluded.updated_at",
        (conv_id, 1 if enabled else 0, int(time.time())),
    )
    conn.commit()
    conn.close()


def get_switches():
    """全部会话的自动回复开关：{conv_id: bool}。没有记录的会话即关闭（安全默认）。"""
    conn = _conn()
    rows = conn.execute("SELECT conv_id, enabled FROM conv_switch").fetchall()
    conn.close()
    return {r["conv_id"]: bool(r["enabled"]) for r in rows}


def list_conversations():
    """会话列表（按最近活跃倒序），每项带 auto_reply=该会话AI自动回复开关（0/1）。"""
    conn = _conn()
    rows = [dict(r) for r in conn.execute(
        "SELECT c.*, COALESCE(s.enabled, 0) AS auto_reply "
        "FROM conversations c "
        "LEFT JOIN conv_switch s ON s.conv_id = c.conv_id "
        "ORDER BY c.last_ts DESC"
    ).fetchall()]
    conn.close()
    return rows


def get_messages(conv_id, limit=300, since_ts=None):
    """取某会话的消息，按时间正序返回。
    - limit 限制的是**最近**多少条（子查询先 DESC 截取再 ASC 排回来）——
      早年写成 ORDER BY ts ASC LIMIT n 取到的是最早的 n 条，是 bug。
    - since_ts：只取该 Unix 时间戳之后的消息（auto_reply 用它做上下文时间窗）。
    """
    conn = _conn()
    sql = ("SELECT * FROM ("
           "SELECT role, text, ts, media, card FROM messages WHERE conv_id=?")
    params = [conv_id]
    if since_ts:
        sql += " AND ts>=?"
        params.append(int(since_ts))
    sql += " ORDER BY ts DESC LIMIT ?) ORDER BY ts ASC"
    params.append(limit)
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    conn.close()
    return rows


# ==================== 朋友圈（moment_ops 的数据落地） ====================

def add_moment(feed, followed=0):
    """落库一条朋友圈动态（按 tid 去重）。返回 1=新增，0=已存在。"""
    conn = _conn()
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO moments "
            "(tid, username, nickname, text, create_time, media, likes, comments, followed, seen_ts) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (str(feed.get("id")), feed.get("username") or "",
             feed.get("nickname") or "", feed.get("text") or "",
             int(feed.get("create_time") or 0),
             _jdump({"images": feed.get("images"), "videos": feed.get("videos")}),
             _jdump(feed.get("likes")), _jdump(feed.get("comments")),
             1 if followed else 0, int(time.time())),
        )
        conn.commit()
        return cur.rowcount or 0
    finally:
        conn.close()


def get_moments(limit=50, only_followed=False, since_ts=None):
    """最近的朋友圈动态（按 create_time 倒序）。"""
    conn = _conn()
    sql = "SELECT * FROM moments"
    params = []
    where = []
    if only_followed:
        where.append("followed=1")
    if since_ts:
        where.append("create_time>=?")
        params.append(int(since_ts))
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY create_time DESC LIMIT ?"
    params.append(limit)
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    conn.close()
    for r in rows:
        r["media_json"] = _jload(r.pop("media", None))
        r["likes_json"] = _jload(r.pop("likes", None))
        r["comments_json"] = _jload(r.pop("comments", None))
    return rows


def add_interaction(it):
    """落库一条「别人赞/评论我」的通知（按 local_id 去重）。返回 1=新增。"""
    conn = _conn()
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO moment_inter "
            "(local_id, type, from_username, from_nickname, content, feed_id, create_time, unread, seen_ts) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (it.get("local_id"), it.get("type") or 0,
             it.get("from_username") or "", it.get("from_nickname") or "",
             it.get("content") or "", str(it.get("feed_id") or ""),
             int(it.get("create_time") or 0), 1, int(time.time())),
        )
        conn.commit()
        return cur.rowcount or 0
    finally:
        conn.close()


def get_interactions(limit=50, only_unread=False):
    """互动通知（赞/评论），按 local_id 倒序。"""
    conn = _conn()
    sql = "SELECT * FROM moment_inter"
    if only_unread:
        sql += " WHERE unread=1"
    sql += " ORDER BY local_id DESC LIMIT ?"
    rows = [dict(r) for r in conn.execute(sql, (limit,)).fetchall()]
    conn.close()
    return rows


def mark_interactions_read():
    """把所有未读互动标为已读，返回变更条数。"""
    conn = _conn()
    try:
        cur = conn.execute("UPDATE moment_inter SET unread=0 WHERE unread=1")
        conn.commit()
        return cur.rowcount or 0
    finally:
        conn.close()


def moment_unread_count():
    conn = _conn()
    try:
        return conn.execute(
            "SELECT count(*) FROM moment_inter WHERE unread=1").fetchone()[0]
    finally:
        conn.close()


def get_moment_cursor():
    """朋友圈轮询水印（字符串：微信 tid 是 20 位大数，超 int64）。"""
    conn = _conn()
    try:
        r = conn.execute("SELECT last_tid FROM moment_cursor WHERE id=1").fetchone()
        return str(r[0]) if r and r[0] is not None else "0"
    finally:
        conn.close()


def set_moment_cursor(last_tid):
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO moment_cursor (id, last_tid) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET last_tid=excluded.last_tid",
            (str(last_tid),))
        conn.commit()
    finally:
        conn.close()


def set_moment_switch(username, auto_like=None, auto_comment=None, nickname=None):
    """设置某客户的朋友圈自动互动开关（None=该项不改）。返回最新状态 dict。

    与 conv_switch 同款语义：无记录=全关；开开关只对之后新到的圈生效，
    不回溯历史（不会批量补赞旧圈）。nickname 只作展示，不影响开关本身。
    """
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT auto_like, auto_comment, nickname FROM moment_switch WHERE username=?",
            (username,)).fetchone()
        like = int(row[0]) if row else 0
        comment = int(row[1]) if row else 0
        nick = (row[2] if row else "") or ""
        if nickname:
            nick = nickname
        if auto_like is not None:
            like = 1 if auto_like else 0
        if auto_comment is not None:
            comment = 1 if auto_comment else 0
        conn.execute(
            "INSERT INTO moment_switch (username, nickname, auto_like, auto_comment, updated_at) "
            "VALUES (?,?,?,?,?) ON CONFLICT(username) DO UPDATE SET "
            "nickname=excluded.nickname, auto_like=excluded.auto_like, auto_comment=excluded.auto_comment, "
            "updated_at=excluded.updated_at",
            (username, nick, like, comment, int(time.time())))
        conn.commit()
        return {"username": username, "nickname": nick,
                "auto_like": like, "auto_comment": comment}
    finally:
        conn.close()


def get_moment_switches():
    """全部客户的朋友圈自动互动开关 {username: {nickname, auto_like, auto_comment}}。"""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT username, nickname, auto_like, auto_comment FROM moment_switch").fetchall()
        return {r[0]: {"nickname": r[1] or "", "auto_like": int(r[2] or 0),
                       "auto_comment": int(r[3] or 0)} for r in rows}
    finally:
        conn.close()


def _jdump(obj):
    import json
    if obj is None:
        return None
    try:
        return json.dumps(obj, ensure_ascii=False)
    except Exception:
        return None


def _jload(s):
    import json
    if not s:
        return None
    try:
        return json.loads(s)
    except Exception:
        return None
