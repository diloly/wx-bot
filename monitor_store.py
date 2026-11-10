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
import json

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
        -- 会话头像（2026-11-06）：微信 contact 表的 small_head_url 直接 GET 下来
        -- 存 wechat_media/avatars/，这里只记「哪张图 + 来源 URL」。src 存原 URL，
        -- 换了头像 URL 就变，据此判断要不要重下。
        CREATE TABLE IF NOT EXISTS avatars (
            conv_id TEXT PRIMARY KEY,
            file    TEXT,
            src     TEXT,
            ts      INTEGER
        );
        -- 微信通讯录全量（2026-11-09）：conversations 只攒「聊过的会话」，
        -- 看板的「+ 选择托管」和激活脚本都需要**所有好友**，否则加了好友
        -- 却从没聊过的人永远选不到、也激活不了。
        CREATE TABLE IF NOT EXISTS contacts (
            conv_id TEXT PRIMARY KEY,   -- wxid / 群号 / 公众号 id
            name    TEXT,               -- 备注优先，其次昵称
            nick    TEXT,
            remark  TEXT,
            kind    TEXT,               -- friend / group / official / system
            ts      INTEGER
        );
        -- 人工接管记录（2026-11-09）：客户有意向 → 通知真人 → 留一条账。
        -- 兼做「同一会话冷却期」判据，防止客户连说两句就刷屏通知内部群。
        CREATE TABLE IF NOT EXISTS handoff_log (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            conv_id  TEXT,               -- 客户会话
            customer TEXT,
            quote    TEXT,               -- 触发接管的客户原话
            reason   TEXT,               -- 判定依据（AI 结论 / 命中关键词）
            targets  TEXT,               -- JSON：本次通知发给了谁
            paused   INTEGER DEFAULT 0,  -- 是否同时关掉了该会话自动回复
            ts       INTEGER
        );
        -- 活动触达（2026-11-10）：人工写死文案 + 指定时间 + 指定对象，到点自动发。
        -- 与 AI 主动触达的区别：不调模型、一次性、内容和名单都由人在看板里定。
        CREATE TABLE IF NOT EXISTS campaign (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT,
            content       TEXT,           -- 固定正文（不经过 AI）
            send_at       INTEGER,        -- 计划发送时刻（Unix 秒）
            targets       TEXT,           -- JSON [{conv_id, name, kind}]
            status        TEXT DEFAULT 'scheduled',
                                          -- scheduled/running/sent/partial/expired/canceled
            result        TEXT DEFAULT '[]',
                                          -- JSON [{conv_id, name, ok, reason, ts}]
            grace_minutes INTEGER DEFAULT 60,
                                          -- 过点多久内还补发；超时作废，防隔夜群发
            created       INTEGER,
            updated       INTEGER
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
    upsert：不存在则插入，存在则更新。

    开启时顺带补一条会话占位（见 ensure_switch_conversations），否则一个
    「加了好友但从没聊过天」的客户开了托管后，左侧列表里不会出现。
    """
    conn = init()
    conn.execute(
        "INSERT INTO conv_switch(conv_id, enabled, updated_at) VALUES(?, ?, ?) "
        "ON CONFLICT(conv_id) DO UPDATE SET enabled=excluded.enabled, updated_at=excluded.updated_at",
        (conv_id, 1 if enabled else 0, int(time.time())),
    )
    conn.commit()
    conn.close()
    if enabled:
        ensure_switch_conversations()


def ensure_switch_conversations():
    """让「已开 AI 托管但从没聊过天」的会话在列表里也有一条占位记录（幂等）。

    conversations 表原本只由 add_message() 在收到消息时创建，所以从「+」
    选择器里给一个从没聊过的客户开托管后，开关写进了 conv_switch，列表却
    查不到这条会话 —— 看板上表现为「托管开了，左侧却没出现」。这里按
    conv_switch 里 enabled=1 的项补占位，顺带把历史上遗留的「幽灵开关」
    一起补上；已有会话一律不覆盖（INSERT OR IGNORE）。

    占位行 last_msg 为空、last_ts 取开关打开时刻：刚开启的会话排在列表最前，
    用户立刻能看到反馈；前端对空 last_msg 的会话不显示时间。主动触达候选靠
    messages 表判断「有没有聊过」，不会被占位行骗到。
    """
    conn = init()
    try:
        # 先只读探测，绝大多数请求（没有缺口）不产生任何写操作
        missing = conn.execute(
            "SELECT 1 FROM conv_switch s "
            "LEFT JOIN conversations c ON c.conv_id = s.conv_id "
            "WHERE s.enabled = 1 AND c.conv_id IS NULL LIMIT 1"
        ).fetchone()
        if not missing:
            return 0
        cur = conn.execute(
            "INSERT OR IGNORE INTO conversations"
            "(conv_id, name, last_msg, last_ts, unread) "
            "SELECT s.conv_id, "
            "  COALESCE(NULLIF(ct.name, ''), NULLIF(ct.nick, ''), "
            "           NULLIF(ct.remark, ''), s.conv_id), "
            "  '', CASE WHEN COALESCE(s.updated_at, 0) > 0 "
            "          THEN s.updated_at ELSE ? END, 0 "
            "FROM conv_switch s "
            "LEFT JOIN contacts ct ON ct.conv_id = s.conv_id "
            "WHERE s.enabled = 1",
            (int(time.time()),),
        )
        conn.commit()
        return cur.rowcount
    finally:
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
    """会话列表（按最近活跃倒序）。

    每项带：auto_reply=该会话AI自动回复开关（0/1）；avatar=头像文件的相对路径
    （wechat_media/ 下，没有则为 None）、avatar_ts=头像落库时间（前端做缓存键）。
    """
    conn = _conn()
    rows = [dict(r) for r in conn.execute(
        "SELECT c.*, COALESCE(s.enabled, 0) AS auto_reply, "
        "a.file AS avatar, a.ts AS avatar_ts "
        "FROM conversations c "
        "LEFT JOIN conv_switch s ON s.conv_id = c.conv_id "
        "LEFT JOIN avatars a ON a.conv_id = c.conv_id "
        "ORDER BY c.last_ts DESC"
    ).fetchall()]
    conn.close()
    return rows


def set_avatar(conv_id, file, src=""):
    """登记某会话的头像（file 是相对 wechat_media/ 的路径，src 是来源 URL）。"""
    conv_id, file = (conv_id or "").strip(), (file or "").strip()
    if not conv_id or not file:
        return False
    conn = init()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO avatars(conv_id, file, src, ts) VALUES(?,?,?,?)",
            (conv_id, file, src or "", int(time.time())))
        conn.commit()
        return True
    finally:
        conn.close()


def get_avatar(conv_id):
    """取某会话的头像记录（{conv_id,file,src,ts}），没有返回 None。"""
    conn = _conn()
    try:
        r = conn.execute(
            "SELECT conv_id, file, src, ts FROM avatars WHERE conv_id=?",
            ((conv_id or "").strip(),)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def upsert_contacts(rows, ts=None):
    """批量写入通讯录（rows: [{conv_id,name,nick,remark,kind}]），返回条数。

    通讯录是「全量覆盖」语义：同一条按 conv_id 覆盖，不删除已消失的联系人
    （微信里删好友不影响这里，看板仍能看到历史；真要清理再单独做）。
    """
    rows = [r for r in (rows or []) if (r or {}).get("conv_id")]
    if not rows:
        return 0
    now = int(ts or time.time())
    conn = init()
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO contacts(conv_id, name, nick, remark, kind, ts) "
            "VALUES(?,?,?,?,?,?)",
            [(r["conv_id"], r.get("name") or "", r.get("nick") or "",
              r.get("remark") or "", r.get("kind") or "friend", now) for r in rows])
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def list_contacts(kind=None):
    """通讯录列表（按名字排序）。kind=friend 只看好友（排除群/公众号/系统号）。"""
    conn = _conn()
    try:
        sql = "SELECT conv_id, name, nick, remark, kind, ts FROM contacts"
        params = []
        if kind:
            sql += " WHERE kind=?"
            params.append(kind)
        sql += " ORDER BY name"
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def get_display_name(conv_id):
    """wxid → 微信 Ctrl+F 用的搜索词（**备注优先，其次昵称**）。

    Ctrl+F 只认微信界面上显示的名字，喂 wxid / 群号搜不到（2026-10-19 首条
    激活事故、2026-11-10 看板手动发送都栽在这里）。contacts.name 由
    sync_contacts 按 `remark or nick` 落库；会话表里的显示名作二级兜底。

    找不到就返回空串 —— 调用方**绝不能**退回去用 wxid 硬搜，那只会把正文
    留在错误的会话里或卡在搜索框，宁可报清楚的错误让人去补备注。
    """
    conn = _conn()
    try:
        r = conn.execute(
            "SELECT name, nick, remark FROM contacts WHERE conv_id=?", (conv_id,)
        ).fetchone()
        if r:
            d = dict(r)
            cand = ((d.get("remark") or "").strip()
                    or (d.get("name") or "").strip()
                    or (d.get("nick") or "").strip())
            if cand and cand != conv_id:
                return cand
        r = conn.execute(
            "SELECT name FROM conversations WHERE conv_id=?", (conv_id,)
        ).fetchone()
        if r:
            cand = (r["name"] or "").strip()
            if cand and cand != conv_id:
                return cand
        return ""
    finally:
        conn.close()


def get_messages(conv_id, limit=300, since_ts=None):
    """取某会话的消息，按时间正序返回。
    - limit 限制的是**最近**多少条（子查询先 DESC 截取再 ASC 排回来）——
      早年写成 ORDER BY ts ASC LIMIT n 取到的是最早的 n 条，是 bug。
    - since_ts：只取该 Unix 时间戳之后的消息（auto_reply 用它做上下文时间窗）。
    """
    conn = _conn()
    sql = ("SELECT * FROM ("
           "SELECT id, role, text, ts, media, card, sent_by FROM messages WHERE conv_id=?")
    params = [conv_id]
    if since_ts:
        sql += " AND ts>=?"
        params.append(int(since_ts))
    sql += " ORDER BY ts DESC LIMIT ?) ORDER BY ts ASC"
    params.append(limit)
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    conn.close()
    return rows


def fix_roles(me_username, dry_run=False):
    """按消息 raw 里的真实发送者重算 ``role``（一次性校正，不删任何消息）。

    什么时候需要（2026-11-06 换设备事故）：桥接早期判「是不是自己发的」用
    ``sender_id == 2``，而 sender_id 是**分片内 Name2Id 的 rowid**，换设备/换账号
    后编号一变，整批消息的 role 就反了——看板左右颠倒、自己的消息被当成客户消息
    反复回复。每条消息入库时 raw 里存了真实的 ``sender_username``，据此可还原。

    只用 raw 判定，**解析不到发送者的行原样保留**（那些行旧判据可能本来是对的）。

    Returns: (scanned, changed, note) —— note 非空表示没执行。
    """
    import ast

    me = (me_username or "").strip()
    if not me:
        return 0, 0, "本机 wxid 为空，无法判定"

    conn = init()
    try:
        rows = conn.execute(
            "SELECT id, role, raw FROM messages WHERE raw LIKE '%sender_username%'"
        ).fetchall()
        scanned = changed = 0
        for r in rows:
            try:
                raw = ast.literal_eval(r["raw"] or "{}")
            except (ValueError, SyntaxError):
                continue
            if not isinstance(raw, dict):
                continue
            su = (raw.get("sender_username") or "").strip()
            if not su:
                continue
            scanned += 1
            want = "me" if su == me else "them"
            if want == r["role"]:
                continue
            changed += 1
            if not dry_run:
                conn.execute("UPDATE messages SET role=? WHERE id=?", (want, r["id"]))
        if changed and not dry_run:
            conn.commit()
        return scanned, changed, ""
    finally:
        conn.close()


def delete_message(msg_id):
    """删一条消息（看板「删错录的消息」用）。删成功返回 True。

    只动看板自己的库，**不碰微信客户端**——微信里那条还在，下次轮询也不会重读
    （poll_cursor 水印已经越过它）。真要连客户端一起删，得在微信里手动撤回/删除。

    删完要**重算该会话的预览行**：`conversations.last_msg/last_ts` 是冗余快照，
    删掉的可能正是最后一条，不重算左侧列表会一直显示已经不存在的那句话。
    """
    try:
        mid = int(msg_id)
    except (TypeError, ValueError):
        return False
    conn = init()
    try:
        row = conn.execute("SELECT conv_id FROM messages WHERE id=?", (mid,)).fetchone()
        if not row:
            return False
        conv_id = row["conv_id"]
        conn.execute("DELETE FROM messages WHERE id=?", (mid,))
        last = conn.execute(
            "SELECT text, ts FROM messages WHERE conv_id=? "
            "ORDER BY ts DESC, id DESC LIMIT 1", (conv_id,)).fetchone()
        if last:
            conn.execute("UPDATE conversations SET last_msg=?, last_ts=? WHERE conv_id=?",
                         (last["text"] or "", last["ts"] or 0, conv_id))
        else:
            conn.execute("UPDATE conversations SET last_msg='', last_ts=0 WHERE conv_id=?",
                         (conv_id,))
        conn.commit()
        return True
    finally:
        conn.close()


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
TASK_DONE = ("sent", "failed", "expired", "handoff")          # 终态


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
# 主动触达（唤醒长期沉默的客户，2026-10-21）
# ---------------------------------------------------------------------------
# 为什么候选全从**我们自己的库**里算，不看朋友圈（用户 2026-10-21 明确）：
# 朋友圈里大量是转发、行业杂讯、无关内容，拿它当搭话由头，十有八九会变成
# 「看到您发的那个…」结果说错对象，比不发更糟。真正可靠的由头只有两类，
# 都在 messages 里：① 对方最后说了一句、我们没接（欠一个回应）；② 上次聊到
# 的具体内容（接着聊）。这两条都不依赖朋友圈。
def get_outreach_candidates(silent_days=7, max_days=60, min_gap_days=14,
                            max_per_contact=3, limit=5, now=None):
    """沉默客户候选池（主动触达用）。

    入选条件：
      · 该会话在看板上**打开了自动回复开关**（conv_switch.enabled=1）——只有
        用户显式授权过的会话才会被主动碰；
      · 私聊（群不做主动触达）；
      · 最后一条消息距今在 [silent_days, max_days] 天：太短不算沉默，太久像骚扰；
      · 我方至少说过一句（从没聊过的交给首条激活，不归这里管）；
      · 累计主动触达次数 < max_per_contact；
      · 距上次主动触达 >= min_gap_days。

    排序：**最后一条是对方说的**排前面——那是「我们欠一个回应」，搭话最自然、
    最不像营销；其余按最近互动时间从新到旧（越近越容易唤醒）。
    """
    now = int(now or time.time())
    lo = now - int(max_days) * 86400
    hi = now - int(silent_days) * 86400
    gap_cutoff = now - int(min_gap_days) * 86400
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT c.conv_id, c.name, c.last_ts, "
            "  (SELECT m.role FROM messages m WHERE m.conv_id=c.conv_id "
            "     ORDER BY m.ts DESC, m.id DESC LIMIT 1) AS last_role, "
            "  (SELECT COUNT(*) FROM messages m WHERE m.conv_id=c.conv_id "
            "     AND m.role='me') AS me_count, "
            "  (SELECT COUNT(*) FROM reply_task t WHERE t.conv_id=c.conv_id "
            "     AND t.kind='outreach' AND t.status='sent' "
            "     AND COALESCE(t.reason,'') NOT LIKE 'dry-run%') AS ou_count, "
            "  (SELECT MAX(t.created) FROM reply_task t WHERE t.conv_id=c.conv_id "
            "     AND t.kind='outreach' AND t.status='sent' "
            "     AND COALESCE(t.reason,'') NOT LIKE 'dry-run%') AS ou_last "
            "FROM conversations c "
            "JOIN conv_switch s ON s.conv_id=c.conv_id AND s.enabled=1 "
            "WHERE c.conv_id NOT LIKE '%@chatroom' "
            "  AND c.last_ts BETWEEN ? AND ? "
            "ORDER BY c.last_ts DESC",
            (lo, hi)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            if int(d.get("me_count") or 0) <= 0:
                continue        # 从没说过话 → 归首条激活，不在这里
            if int(d.get("ou_count") or 0) >= int(max_per_contact):
                continue        # 碰过太多次了，停手
            if int(d.get("ou_last") or 0) > gap_cutoff:
                continue        # 冷却期内
            out.append(d)
        out.sort(key=lambda d: (0 if d.get("last_role") == "them" else 1,
                                -(d.get("last_ts") or 0)))
        return out[:int(limit)]
    finally:
        conn.close()


def get_activation_targets(silent_hours=24, max_days=0, min_gap_days=7,
                           include_no_chat=False, require_switch=False,
                           limit=50, now=None, max_per_contact=0):
    """存量客户「激活」的候选池（activate.py 用，与 get_outreach_candidates 的区别见下）。

    和主动触达的区别：
      · 沉默门槛按**小时**算（默认 24h），不像触达要 7 天——用户要「从现在开始激活」；
      · 不要求「我方说过话」，因为可选把**加了好友从没聊过**的人一起算进来（破冰）；
      · 默认不要求会话开关（脚本本身就是显式的人工指令），require_switch=True 才卡。

    入选条件：私聊 → 最后一条距今 ≥ silent_hours → （可选）不超过 max_days →
    距上次 outreach 类动作 ≥ min_gap_days → 累计消息数 > 0（revive 类型）→
    累计触达次数 < max_per_contact（0 = 不限）。

    Returns: [{conv_id, name, kind:'revive'|'icebreak', last_ts, last_role, msg_count}]
    """
    now = int(now or time.time())
    hi = now - int(float(silent_hours) * 3600)
    lo = (now - int(max_days) * 86400) if max_days else 0
    gap_cutoff = now - int(min_gap_days) * 86400

    out = []
    me = (get_setting("me_wxid") or "").strip()   # 通讯录里也有自己，别给自己发消息
    conn = _conn()
    try:
        # 只碰「真正的好友」：conversations 里同时混着文件传输助手、微信团队、
        # 公众号这些系统会话，给它们发"激活"既没意义也显得很傻。
        # 通讯录同步过（contacts 非空）就以 kind='friend' 为准；没同步过则退回
        # 「排除明显的系统号/公众号」的兜底判断，不至于一条候选都没有。
        has_contacts = conn.execute("SELECT 1 FROM contacts LIMIT 1").fetchone() is not None
        sql = ("SELECT c.conv_id, c.name, c.last_ts, "
               "  (SELECT m.role FROM messages m WHERE m.conv_id=c.conv_id "
               "     ORDER BY m.ts DESC, m.id DESC LIMIT 1) AS last_role, "
               "  (SELECT COUNT(*) FROM messages m WHERE m.conv_id=c.conv_id) AS msg_count, "
               "  (SELECT MAX(t.created) FROM reply_task t WHERE t.conv_id=c.conv_id "
               "     AND t.kind='outreach' AND t.status='sent' "
               "     AND COALESCE(t.reason,'') NOT LIKE 'dry-run%') AS act_last, "
               "  (SELECT COUNT(*) FROM reply_task t WHERE t.conv_id=c.conv_id "
               "     AND t.kind='outreach' AND t.status='sent' "
               "     AND COALESCE(t.reason,'') NOT LIKE 'dry-run%') AS act_count, "
               "  COALESCE(s.enabled, 0) AS switched "
               "FROM conversations c "
               "LEFT JOIN conv_switch s ON s.conv_id=c.conv_id "
               "WHERE c.conv_id NOT LIKE '%@chatroom' AND c.last_ts <= ?")
        params = [hi]
        if has_contacts:
            sql += (" AND EXISTS (SELECT 1 FROM contacts ct WHERE ct.conv_id=c.conv_id"
                    " AND ct.kind='friend')")
        else:
            sql += (" AND substr(c.conv_id, 1, 3) <> 'gh_'"
                    " AND c.conv_id NOT IN ('filehelper','weixin','fmessage','medianote',"
                    "'floatbottle','newsapp','notifymessage','qqmail','qqsafe')")
        if lo:
            sql += " AND c.last_ts >= ?"
            params.append(lo)
        if me:
            sql += " AND c.conv_id <> ?"
            params.append(me)
        sql += " ORDER BY c.last_ts DESC"
        for r in conn.execute(sql, params).fetchall():
            d = dict(r)
            if int(d.get("msg_count") or 0) <= 0:
                continue                    # 没消息的交给 icebreak 分支
            if int(d.get("act_last") or 0) > gap_cutoff:
                continue                    # 冷却期内，别反复打扰
            if max_per_contact and int(d.get("act_count") or 0) >= int(max_per_contact):
                continue                    # 累计触达次数到顶，这辈子不再自动碰他
            if require_switch and not int(d.get("switched") or 0):
                continue
            out.append({
                "conv_id": d["conv_id"],
                "name": (d.get("name") or d["conv_id"]),
                "kind": "revive",
                "last_ts": int(d.get("last_ts") or 0),
                "last_role": d.get("last_role") or "",
                "msg_count": int(d.get("msg_count") or 0),
            })
    finally:
        conn.close()

    if include_no_chat:
        conn = _conn()
        try:
            sql2 = ("SELECT ct.conv_id, ct.name, "
                    "  (SELECT MAX(t.created) FROM reply_task t WHERE t.conv_id=ct.conv_id "
                    "     AND t.kind='outreach' AND t.status='sent' "
                    "     AND COALESCE(t.reason,'') NOT LIKE 'dry-run%') AS act_last, "
                    "  (SELECT COUNT(*) FROM reply_task t WHERE t.conv_id=ct.conv_id "
                    "     AND t.kind='outreach' AND t.status='sent' "
                    "     AND COALESCE(t.reason,'') NOT LIKE 'dry-run%') AS act_count "
                    "FROM contacts ct "
                    "WHERE ct.kind='friend' AND NOT EXISTS ("
                    # 判「从没聊过」要看 messages，不能看 conversations：
                    # 开了托管但没聊过的人现在也会有一条占位会话记录。
                    "  SELECT 1 FROM messages m WHERE m.conv_id=ct.conv_id)")
            p2 = []
            if me:                          # 通讯录里有自己，别给自己发破冰消息
                sql2 += " AND ct.conv_id <> ?"
                p2.append(me)
            sql2 += " ORDER BY ct.name"
            rows = conn.execute(sql2, p2).fetchall()
        finally:
            conn.close()
        if not require_switch:              # 没会话就没有开关，「要求开关」时这批直接不要
            for r in rows:
                d = dict(r)
                if int(d.get("act_last") or 0) > gap_cutoff:
                    continue
                if max_per_contact and int(d.get("act_count") or 0) >= int(max_per_contact):
                    continue
                out.append({
                    "conv_id": d["conv_id"],
                    "name": (d.get("name") or d["conv_id"]),
                    "kind": "icebreak",
                    "last_ts": 0, "last_role": "", "msg_count": 0,
                })
    return out[:int(limit)]


def count_outreach_since(since_ts):
    """某时刻之后**已真实发出**的主动触达条数（每日预算用）。

    dry-run 的预演不算——否则开着 dry_run 预览几条，等真开了额度已经用光了。
    预演任务的状态同样是 'sent'，只能靠 reason 里的 'dry-run' 前缀区分。
    """
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM reply_task WHERE kind='outreach' "
            "AND status='sent' AND created>=? "
            "AND COALESCE(reason,'') NOT LIKE 'dry-run%'",
            (int(since_ts),)).fetchone()
        return int(row["n"] or 0)
    finally:
        conn.close()


def last_outreach_ts():
    """最近一次主动触达任务的创建时刻（全局间隔闸用）；没有返回 0。

    只看 sent 不够：正在生成/发送中的那一条也该把间隔占住，否则连点两次
    会给同一个人排两条。
    """
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT MAX(created) AS t FROM reply_task WHERE kind='outreach' "
            "AND status IN ('pending','generating','generated','sending','sent')"
        ).fetchone()
        return int(row["t"] or 0)
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


# ---------------------------------------------------------------------------
# 人工接管（客户有意向 → 通知真人，2026-11-09）
# ---------------------------------------------------------------------------
def has_recent_outreach(conv_id, since_ts):
    """这个会话最近有没有被我们主动激活/触达过。

    「激活后客户才回的话」是接管检测的重点：只对这类会话做意向判定，
    既贴合业务，也避免每条普通消息都多调一次模型。
    """
    conn = init()          # 新库第一次用到时自动建 handoff_log
    try:
        row = conn.execute(
            "SELECT 1 FROM reply_task WHERE conv_id=? AND kind='outreach' "
            "AND status='sent' AND created>=? "
            "AND COALESCE(reason,'') NOT LIKE 'dry-run%' LIMIT 1",
            (conv_id, int(since_ts))).fetchone()
        return row is not None
    finally:
        conn.close()


def last_handoff_ts(conv_id):
    conn = init()
    try:
        row = conn.execute("SELECT MAX(ts) AS t FROM handoff_log WHERE conv_id=?",
                           (conv_id,)).fetchone()
        return int(row["t"] or 0)
    finally:
        conn.close()


def add_handoff_log(conv_id, customer, quote, reason, targets, paused, ts=None):
    import json
    conn = init()
    try:
        cur = conn.execute(
            "INSERT INTO handoff_log(conv_id, customer, quote, reason, targets, paused, ts) "
            "VALUES(?,?,?,?,?,?,?)",
            (conv_id, customer, quote, reason,
             json.dumps(targets or [], ensure_ascii=False), 1 if paused else 0,
             int(ts or time.time())))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_handoff_log(limit=30):
    conn = init()
    try:
        rows = conn.execute(
            "SELECT * FROM handoff_log ORDER BY ts DESC LIMIT ?",
            (int(limit),)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 活动触达（固定文案 + 指定时间 + 指定对象，2026-11-10）
# ---------------------------------------------------------------------------
def _json(s, fallback):
    try:
        v = json.loads(s or "")
        return v if isinstance(v, type(fallback)) else fallback
    except Exception:               # noqa: BLE001
        return fallback


def add_campaign(name, content, send_at, targets, grace_minutes=60):
    conn = init()
    try:
        now = int(time.time())
        cur = conn.execute(
            "INSERT INTO campaign(name, content, send_at, targets, status, result, "
            "grace_minutes, created, updated) VALUES(?,?,?,?, 'scheduled', '[]', ?,?,?)",
            (name, content, int(send_at),
             json.dumps(targets or [], ensure_ascii=False),
             int(grace_minutes), now, now))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_campaigns(limit=50):
    conn = init()
    try:
        rows = conn.execute(
            "SELECT * FROM campaign ORDER BY send_at DESC, id DESC LIMIT ?",
            (int(limit),)).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["targets"] = _json(d.get("targets"), [])
        d["result"] = _json(d.get("result"), [])
        out.append(d)
    return out


def get_campaign(cid):
    for c in list_campaigns(1000):
        if c["id"] == int(cid):
            return c
    return None


def claim_campaign(cid):
    """抢占一条待发活动（条件 UPDATE，保证多进程/多轮巡检只投一次）。"""
    conn = init()
    try:
        cur = conn.execute(
            "UPDATE campaign SET status='running', updated=? "
            "WHERE id=? AND status='scheduled'",
            (int(time.time()), int(cid)))
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def due_campaigns(now=None):
    """到点且仍在宽限期内的活动；同时把过点的标 expired（绝不隔夜群发）。"""
    now = int(now or time.time())
    conn = init()
    try:
        conn.execute(
            "UPDATE campaign SET status='expired', updated=? "
            "WHERE status='scheduled' "
            "AND (? - send_at) > grace_minutes * 60",
            (now, now))
        # 进程重启后残留的 running（没投完就退出）标 partial，不再自动重投
        conn.execute(
            "UPDATE campaign SET status='partial', updated=? "
            "WHERE status='running' AND updated < ?",
            (now, now - 1800))
        conn.commit()
        rows = conn.execute(
            "SELECT * FROM campaign WHERE status='scheduled' AND send_at<=? "
            "ORDER BY send_at ASC LIMIT 10", (now,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def record_campaign_result(cid, conv_id, name, ok, reason=""):
    """回写单个对象的发送结果；全部回齐后把活动收口成 sent / partial。"""
    conn = init()
    try:
        row = conn.execute("SELECT targets, result FROM campaign WHERE id=?",
                           (int(cid),)).fetchone()
        if not row:
            return
        targets = _json(row["targets"], [])
        results = _json(row["result"], [])
        results = [r for r in results if r.get("conv_id") != conv_id]
        results.append({"conv_id": conv_id, "name": name, "ok": bool(ok),
                        "reason": str(reason or "")[:160], "ts": int(time.time())})
        status = "running"
        if len(results) >= len(targets):
            status = "sent" if all(r["ok"] for r in results) else "partial"
        conn.execute("UPDATE campaign SET result=?, status=?, updated=? WHERE id=?",
                     (json.dumps(results, ensure_ascii=False), status,
                      int(time.time()), int(cid)))
        conn.commit()
    finally:
        conn.close()


def cancel_campaign(cid):
    conn = init()
    try:
        cur = conn.execute(
            "UPDATE campaign SET status='canceled', updated=? "
            "WHERE id=? AND status='scheduled'", (int(time.time()), int(cid)))
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def send_campaign_now(cid):
    """把计划时间改成现在，下一轮巡检（≤20s）就会真发。"""
    conn = init()
    try:
        cur = conn.execute(
            "UPDATE campaign SET send_at=?, updated=? "
            "WHERE id=? AND status='scheduled'",
            (int(time.time()), int(time.time()), int(cid)))
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def delete_campaign(cid):
    conn = init()
    try:
        cur = conn.execute("DELETE FROM campaign WHERE id=?", (int(cid),))
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()
