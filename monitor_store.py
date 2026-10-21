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
import re
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
            card    TEXT,           -- 链接/文件卡片 JSON（title/descr/url/thumb/kind）
            sent_by TEXT            -- 'ai' 自动回复 | 'manual' 看板手发 | NULL 未知（历史/手机端）
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
        CREATE TABLE IF NOT EXISTS reply_task (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            kind     TEXT DEFAULT 'reply',            -- 'reply'=会话回复；'moment' 预留
            conv_id  TEXT,
            name     TEXT,
            text     TEXT,                            -- 触发任务的客户原话
            status   TEXT,                            -- 见下方 TASK_* 状态机
            reply    TEXT,                            -- AI 生成结果
            reason   TEXT,                            -- 失败/作废原因（面板展示）
            created  INTEGER,                         -- 投递时刻
            deadline INTEGER,                         -- 任务预算截止（created + task_timeout）
            updated  INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_task_status ON reply_task(status, id);
        CREATE INDEX IF NOT EXISTS idx_task_updated ON reply_task(updated);
        CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(conv_id, ts);
        -- 加好友名单/队列（2026-10-19）
        -- 一张表既是名单也是队列：CSV 导入 → pending → worker 逐个处理 → sent。
        -- status: pending / sent / passed / rejected / already / failed / expired
        CREATE TABLE IF NOT EXISTS friend_task (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            keyword   TEXT NOT NULL,                  -- 手机号或微信号（搜索框认这两种）
            name      TEXT,                           -- 称呼/备注名，日志与话术用
            note      TEXT,                           -- 背景：哪来的、聊过什么（喂 AI）
            source    TEXT,                           -- 工作坊/转介绍/展会（挑申请语变体）
            status    TEXT DEFAULT 'pending',
            reason    TEXT,                           -- 失败原因（面板展示）
            conv_id   TEXT,                           -- 通过后回填的 username
            sent_at   INTEGER,                        -- 发出申请的时刻
            passed_at INTEGER,                        -- 检测到通过的時刻
            tries     INTEGER DEFAULT 0,              -- 技术失败重试次数
            created   INTEGER,
            updated   INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_friend_status ON friend_task(status, id);
        CREATE INDEX IF NOT EXISTS idx_friend_sent ON friend_task(sent_at);
        -- 运行期开关（看板改、auto_reply 每轮读）。为什么不写 config.json：
        -- 那是另一个进程启动时读的，改了要重启才生效；开关要「点一下立刻停」。
        CREATE TABLE IF NOT EXISTS app_setting (
            k TEXT PRIMARY KEY,
            v TEXT
        );
        """
    )
    # 老库可能没有 msg_hash / media / card / sent_by 列，补齐（ALTER 不支持 IF NOT EXISTS，用异常吞掉已存在）
    for col in ("msg_hash", "media", "card", "sent_by"):
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
    # 一次性回填：sent_by 是 2026-10-19 才加的列，此前的自动回复都没有标记。
    # 用 reply_task 账本反查（同会话 + 文案完全相同）把历史 AI 回复补成 'ai'。
    # 只补 IS NULL 的、只认完全一致的文案——宁可留空也不乱标（人工发的没账本，
    # 补不上就保持 NULL，看板不显示标记）。幂等：补过的不会再命中。
    try:
        cur = conn.execute(
            "UPDATE messages SET sent_by='ai' "
            "WHERE role='me' AND sent_by IS NULL AND EXISTS ("
            "  SELECT 1 FROM reply_task t "
            "  WHERE t.conv_id = messages.conv_id AND t.reply = messages.text)"
        )
        if cur.rowcount and cur.rowcount > 0:
            print("[monitor_store] 已回填 %d 条历史消息的发送方标记（sent_by=ai）"
                  % cur.rowcount)
    except sqlite3.OperationalError:
        pass
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
                media=None, card=None, sent_by=None, dedupe_loose=False):
    """写入一条消息。msg_key 用于去重（如微信 MsgSvrID）；缺省则用 会话+角色+时间+内容 推算。
    media=媒体文件相对路径（wechat_media/ 下）；card=链接/文件卡片 JSON。
    sent_by：role='me' 时标注是谁发的——'ai'（auto_reply 自动回复）/ 'manual'
    （看板手发）；收消息桥与手机端发的不传，保持 NULL（看板不显示标记）。
    dedupe_loose：额外按 (会话+时间+角色+文本) 判重。收消息桥用——msg_key 的
    算法在 2026-10-19 换过（加了 create_time），老记录的 msg_hash 是旧算法算的，
    重建基线时算不出同一个 hash，会把这些消息当新的重复入库。
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
    # 宽松二次判重：见 dedupe_loose 说明。索引 idx_msg_conv(conv_id, ts) 覆盖。
    if dedupe_loose and cur.execute(
        "SELECT 1 FROM messages WHERE conv_id=? AND ts=? AND role=? AND text=? LIMIT 1",
        (conv_id, ts, role, text),
    ).fetchone():
        conn.close()
        return False
    cur.execute(
        "INSERT OR IGNORE INTO messages(conv_id, role, text, ts, raw, msg_hash, media, card, sent_by) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (conv_id, role, text, ts, raw, h, media, card, sent_by),
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


_WS_RE = re.compile(r"\s+")


def _norm_sent(text):
    """比较用的归一：把空白全部压掉。

    为什么需要（2026-10-21）：群聊把 @ 和正文合并成一条后，微信会把 @ 提醒存成
    `@昵称\\u2005正文`（U+2005 四分之一空格），而我们这边记的是普通空格；严格
    相等对不上，收消息桥就会把这条**自己发的**消息当成新消息再录一遍，看板同一
    句出现两条。这里跟「是不是自己发的」无关，只做文本比对前的清洗。
    """
    return _WS_RE.sub("", text or "")


def was_sent(conv_id, text, within_seconds=120):
    """最近 within_seconds 秒内程序是否给该会话发过同文本消息。

    比较前两侧都做 `_norm_sent` 归一（见上）。放宽比较不会漏掉别人的消息——
    这张表里只有本程序自己发出去的东西。
    """
    cutoff = int(time.time()) - within_seconds
    target = _norm_sent(text)
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT text FROM sent_log WHERE conv_id=? AND ts>=?",
            (conv_id, cutoff),
        ).fetchall()
    finally:
        conn.close()
    return any(_norm_sent(r["text"]) == target for r in rows)


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


def reset_cursor(conv_id=None):
    """删除某会话（conv_id=None 则全部）的轮询水印，下一轮 poll_new 会重建基线。
    用于「消息被去重挡掉、水印却已经推过去」的补救（如微信重建会话）。"""
    conn = init()
    if conv_id:
        cur = conn.execute("DELETE FROM poll_cursor WHERE conv_id=?", (conv_id,))
    else:
        cur = conn.execute("DELETE FROM poll_cursor")
    n = cur.rowcount or 0
    conn.commit()
    conn.close()
    return n


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


def set_conversation_name(conv_id, name):
    """更新会话显示名（微信端改过备注/昵称后，由收消息桥同步过来）。

    为什么单独开一个函数：conversations.name 原来只由 add_message(name=...)
    在「该会话有新消息」时更新，用户手动改备注不会产生任何消息，看板就会一直
    显示改名前的旧名字（2026-10-21 用户反馈）。
    """
    conn = _conn()
    try:
        conn.execute("UPDATE conversations SET name=? WHERE conv_id=?",
                     (name, conv_id))
        conn.commit()
    finally:
        conn.close()


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
           "SELECT role, text, ts, media, card, sent_by FROM messages WHERE conv_id=?")
    params = [conv_id]
    if since_ts:
        sql += " AND ts>=?"
        params.append(int(since_ts))
    sql += " ORDER BY ts DESC LIMIT ?) ORDER BY ts ASC"
    params.append(limit)
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    conn.close()
    return rows


def recent_messages_matching(patterns, since_ts, limit=200):
    """找 since_ts 之后、文本命中任一 pattern 的消息（按 ts 正序返回）。

    用途：启动时补跑「好友通过」事件。这类事件原本只在消息**新入库那一刻**
    被 auto_reply 的 collect 触发，进程没开、或归属判定把事件吞掉，这次通过
    就永久丢了——客户通过了却永远收不到第一条消息。
    """
    pats = [p for p in (patterns or []) if p]
    if not pats:
        return []
    where = " OR ".join(["text LIKE ?"] * len(pats))
    params = ["%" + p + "%" for p in pats]
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT conv_id, role, text, ts FROM messages "
            "WHERE ts>=? AND (%s) ORDER BY ts ASC LIMIT ?" % where,
            [int(since_ts)] + params + [int(limit)],
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


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


# ==================== 维护任务队列（看板可观测） ====================
# 2026-10-16：auto_reply 的「待生成 / 待发送」两个内存队列落库成这张表。
# 内存 queue 仍是实际的工作队列（worker 靠它阻塞取值、保证 FIFO），这张表是
# **状态账本**——两个进程不共享内存，看板（另一个进程）只能通过库看见队列。
# 不做控制（取消/插队）：只读展示。
# 状态机：pending → generating → generated → sending → sent
#         任一环节失败/超时 → failed / expired（终态）
TASK_OPEN = ("pending", "generating", "generated", "sending")   # 未终态
TASK_DONE = ("sent", "failed", "expired")                       # 终态


def add_task(conv_id, name, text, deadline, kind="reply", created=None):
    """投递一条维护任务（落库，状态 pending），返回任务 id。"""
    now = int(created or time.time())
    conn = init()
    try:
        cur = conn.execute(
            "INSERT INTO reply_task(kind, conv_id, name, text, status, "
            "created, deadline, updated) VALUES(?, ?, ?, ?, 'pending', ?, ?, ?)",
            (kind, conv_id, name, text, now, int(deadline), now))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def update_task(task_id, status=None, reply=None, reason=None):
    """更新任务状态。只传要改的字段（None 表示不动）。"""
    sets, params = ["updated=?"], [int(time.time())]
    if status is not None:
        sets.append("status=?")
        params.append(status)
    if reply is not None:
        sets.append("reply=?")
        params.append(reply)
    if reason is not None:
        sets.append("reason=?")
        params.append(reason)
    params.append(int(task_id))
    conn = init()
    try:
        conn.execute("UPDATE reply_task SET %s WHERE id=?" % ", ".join(sets), params)
        conn.commit()
    finally:
        conn.close()


def get_tasks(limit=30):
    """最近的任务（新→旧），供看板展示。"""
    conn = _conn()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT id, kind, conv_id, name, text, status, reply, reason, "
            "created, deadline, updated FROM reply_task ORDER BY id DESC LIMIT ?",
            (int(limit),)).fetchall()]
    finally:
        conn.close()


def task_counts():
    """各状态计数 + open（未终态合计）。看板顶部条用。"""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS c FROM reply_task GROUP BY status").fetchall()
    finally:
        conn.close()
    out = {r["status"]: r["c"] for r in rows}
    for s in TASK_OPEN + TASK_DONE:
        out.setdefault(s, 0)
    out["open"] = sum(out.get(s, 0) for s in TASK_OPEN)
    return out


def expire_open_tasks(reason="进程重启，任务作废"):
    """启动清理：把上次运行残留的未完成任务标为 expired，返回作废条数。

    为什么必须做（而不只是"可选"）：内存队列随进程一起没了，库里那些
    pending/generating 再也没人会动；不清理，看板会永远显示一堆假的"进行中"，
    「看得见」这个需求本身就失真了。
    为什么不做「补跑」：旧回复早已过时，补跑还可能与重启后新收的消息任务重叠
    （同一个客户被回两次）。故一律作废，交人工。
    """
    conn = init()
    try:
        qs = ",".join("?" * len(TASK_OPEN))
        cur = conn.execute(
            "UPDATE reply_task SET status='expired', reason=?, updated=? "
            "WHERE status IN (%s)" % qs,
            (reason, int(time.time())) + TASK_OPEN)
        conn.commit()
        return cur.rowcount or 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 加好友：名单 / 队列 / 通过检测（2026-10-19）
#
# 状态机：pending → sent → passed
#                 └→ failed（技术执行失败，可重试）
#         already（已在通讯录，跳过） / rejected（对方拒绝） / expired（长期未通过）
#
# **「未通过」不是失败**：发完即走，状态停在 sent，不占队列、不阻塞。
# failed 只表示这一次的技术执行挂了（搜不到人/找不到按钮/界面异常）。
# ---------------------------------------------------------------------------
FRIEND_OPEN = ("pending", "sent")
FRIEND_DONE = ("passed", "rejected", "already", "failed", "expired")
_FRIEND_COLS = {"keyword", "name", "note", "source", "status", "reason",
                "conv_id", "sent_at", "passed_at", "tries"}


def add_friend_task(keyword, name=None, note=None, source=None, retry_days=30):
    """把一条名单入队。返回 (id, 跳过原因)；成功入队时原因为 None。

    去重规则（直接决定会不会给同一个人发两次申请）：
      ① 同关键词已有 pending/sent → 跳过（还在流程里）；
      ② 同关键词在 retry_days 天内 failed/expired/rejected → 跳过（别反复骚扰）。
    已 passed/already 的不拦：那是很久以前的事，用户可能想再联系（由人判断）。
    """
    kw = (keyword or "").strip()
    if not kw:
        return None, "空关键词"
    now = int(time.time())
    conn = init()
    try:
        row = conn.execute(
            "SELECT id, status FROM friend_task WHERE keyword=? AND status IN ('pending','sent') "
            "ORDER BY id DESC LIMIT 1", (kw,)).fetchone()
        if row:
            return row["id"], "已在队列中(%s)" % row["status"]
        since = now - int(retry_days) * 86400
        row = conn.execute(
            "SELECT id, status FROM friend_task WHERE keyword=? AND created>=? "
            "AND status IN ('failed','expired','rejected') ORDER BY id DESC LIMIT 1",
            (kw, since)).fetchone()
        if row:
            return row["id"], "%d 天内失败过(%s)" % (int(retry_days), row["status"])
        cur = conn.execute(
            "INSERT INTO friend_task(keyword, name, note, source, status, tries, "
            "created, updated) VALUES(?, ?, ?, ?, 'pending', 0, ?, ?)",
            (kw, name, note, source, now, now))
        conn.commit()
        return cur.lastrowid, None
    finally:
        conn.close()


def update_friend_task(task_id, **kw):
    """按字段更新（只认 _FRIEND_COLS 里的列，其余静默忽略）。"""
    sets, params = ["updated=?"], [int(time.time())]
    for k, v in kw.items():
        if k in _FRIEND_COLS:
            sets.append("%s=?" % k)
            params.append(v)
    params.append(int(task_id))
    conn = init()
    try:
        conn.execute("UPDATE friend_task SET %s WHERE id=?" % ", ".join(sets), params)
        conn.commit()
    finally:
        conn.close()


def get_friend_pending(limit=1):
    """取最老的 pending 名单项（FIFO）。"""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT id, keyword, name, note, source, tries FROM friend_task "
            "WHERE status='pending' ORDER BY id ASC LIMIT ?", (int(limit),)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_friend_sent(limit=5):
    """最近的已发申请（用于把「通过」系统消息匹配回名单项）。"""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT id, keyword, name, note, source, sent_at FROM friend_task "
            "WHERE status='sent' ORDER BY sent_at DESC LIMIT ?", (int(limit),)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_friend_passed_pending_touch(limit=20, days=7):
    """已通过、但首条激活**一条都没排过**的名单项（启动补排用）。

    为什么要它：首条激活失败（搜索词错、UI 排队超时、AI 生成失败）时任务落
    failed，而 friend_task 早就是 passed 了——没人补排，这个客户就永远不会被
    激活，用户只能删好友重加才能再试一次。

    幂等靠 NOT EXISTS：**只要这个会话存在过任何一条 kind='first_touch' 的任务
    就不再返回**。days 限制只补最近通过的那批，避免把一个月前的人突然翻出来
    打招呼。

    为什么是「排过就不管」，而不是「发成功才不管」（2026-10-21 改）：
    旧判据是 `status='sent' OR reason LIKE '%无法确认%'` 才排除，结果 AI 侧失败
    （finish_reason=length 那类）的客户**每次重启都被重新排一遍**——同一个人
    10-19 16:59、10-21 09:42、10-21 11:29 排了三次，三次都因同一个原因失败，
    面板上看起来就像「这个人的首次激活永远在失败」。补排应该只负责「漏掉的
    那次」，失败了就该停下来交人工——自动无限重试既有重复打扰客户的风险
    （部分失败其实是发出去了、只是没确认到），也不符合「失败不自动重试」的
    既定原则。要再试一次，看板上手动发。

    「无法确认」那类（`reason LIKE '%无法确认%'`：消息可能已经发出去了、只是
    读库确认不了）也一并落在这个 NOT EXISTS 里——那种更不能自动补，赌它没发出
    就是给客户发两遍，留给人工核对。
    """
    conn = _conn()
    try:
        cutoff = int(time.time()) - int(days) * 86400
        rows = conn.execute(
            "SELECT f.id, f.keyword, f.name, f.note, f.source, f.conv_id, f.passed_at "
            "FROM friend_task f "
            "WHERE f.status='passed' AND f.conv_id IS NOT NULL AND f.conv_id<>'' "
            "  AND COALESCE(f.passed_at, 0) > ? "
            "  AND NOT EXISTS (SELECT 1 FROM reply_task t WHERE t.conv_id=f.conv_id "
            "                  AND t.kind='first_touch') "
            "ORDER BY f.passed_at ASC LIMIT ?",
            (cutoff, int(limit))).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_friend_tasks(limit=50):
    """看板展示用（新→旧）。"""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT id, keyword, name, note, source, status, reason, conv_id, "
            "sent_at, passed_at, tries, created, updated FROM friend_task "
            "ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def friend_counts():
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS c FROM friend_task GROUP BY status").fetchall()
    finally:
        conn.close()
    out = {r["status"]: r["c"] for r in rows}
    for s in FRIEND_OPEN + FRIEND_DONE:
        out.setdefault(s, 0)
    return out


def friend_sent_today(day=None):
    """今天已发出的申请数（按 sent_at 落在当天 0 点之后算）。

    直接数 friend_task 行，不用额外计数器——计数器会漂移，行不会。
    """
    import datetime
    d = day or datetime.date.today()
    start = int(datetime.datetime(d.year, d.month, d.day).timestamp())
    conn = _conn()
    try:
        return conn.execute(
            "SELECT COUNT(*) AS c FROM friend_task WHERE sent_at>=? "
            "AND status IN ('sent','passed')", (start,)).fetchone()["c"]
    finally:
        conn.close()


def conv_existed_before(conv_id, ts):
    """这个会话在 ts 之前是否已有消息（= 是老联系人，不是刚通过的新好友）。"""
    conn = _conn()
    try:
        return conn.execute(
            "SELECT 1 FROM messages WHERE conv_id=? AND ts<? LIMIT 1",
            (conv_id, int(ts))).fetchone() is not None
    finally:
        conn.close()


def expire_stale_friend_tasks(days=7, reason="超过 %d 天未通过"):
    """把久未通过的 sent 标 expired。只在启动时调一次，幂等。"""
    import datetime
    cutoff = int((datetime.datetime.now()
                  - datetime.timedelta(days=int(days))).timestamp())
    conn = init()
    try:
        cur = conn.execute(
            "UPDATE friend_task SET status='expired', reason=?, updated=? "
            "WHERE status='sent' AND sent_at<?",
            (reason % int(days), int(time.time()), cutoff))
        conn.commit()
        return cur.rowcount or 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 运行期开关（看板 ↔ auto_reply 跨进程，不写 config.json）
# ---------------------------------------------------------------------------
def get_setting(k, default=None):
    conn = _conn()
    try:
        row = conn.execute("SELECT v FROM app_setting WHERE k=?", (k,)).fetchone()
        return row["v"] if row else default
    finally:
        conn.close()


def set_setting(k, v):
    conn = init()
    try:
        conn.execute("INSERT OR REPLACE INTO app_setting(k, v) VALUES(?, ?)",
                     (k, "" if v is None else str(v)))
        conn.commit()
    finally:
        conn.close()
