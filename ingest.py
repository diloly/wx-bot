#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从 PyWxDump 解密后的微信 DB 导入聊天记录到统一消息库（wechat_monitor.db）。

PyWxDump 用法（在你本机 Windows 上）：
    pip install pywxdump
    python -m pywxdump decrypt <微信数据目录>      # 产出解密后的 *.db
本脚本读解密后的 SQLite（含 MSG 表的那种）。

用法：
    python ingest.py --probe  <解密db路径>     # 先看表/字段，确认列名
    python ingest.py --source <解密db路径>      # 批量导入
支持：传入单个 .db 文件，或传入包含 *.db 的目录（自动挑含 MSG 表的那个）。
列名做了兼容兜底（strTalker/strContent/CreateTime/IsSender 等多种写法）。
"""
import os
import sqlite3
import argparse
import time

from monitor_store import init, add_message


def _open(path):
    return sqlite3.connect(path)


def find_msg_db(source):
    """返回 (path, table_name)。source 可为文件或目录。"""
    if os.path.isfile(source):
        return source, _pick_msg_table(source)
    for root, _, files in os.walk(source):
        for f in files:
            if f.lower().endswith(".db"):
                p = os.path.join(root, f)
                t = _pick_msg_table(p)
                if t:
                    return p, t
    raise FileNotFoundError(f"在 {source} 下没找到含 MSG 表的数据库")


def _pick_msg_table(db_path):
    try:
        conn = _open(db_path)
        tabs = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        for t in tabs:
            if t.upper() == "MSG":
                conn.close()
                return t
        for t in tabs:
            cols = [c[1] for c in conn.execute(f"PRAGMA table_info('{t}')").fetchall()]
            if "strContent" in cols:
                conn.close()
                return t
        conn.close()
    except Exception:
        return None
    return None


def probe(source):
    path, msg_table = find_msg_db(source)
    conn = _open(path)
    tabs = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    print(f"选定 MSG 表: {msg_table}  (文件: {path})")
    print("全部表:", tabs)
    for t in tabs:
        cols = [c[1] for c in conn.execute(f"PRAGMA table_info('{t}')").fetchall()]
        cnt = conn.execute(f"SELECT COUNT(*) FROM '{t}'").fetchone()[0]
        print(f"\n[{t}] 行数={cnt}")
        print("  字段:", cols)
        if cnt:
            row = conn.execute(f"SELECT * FROM '{t}' LIMIT 1").fetchone()
            print("  样例:", dict(zip(cols, row)))
    conn.close()


def build_name_map(conn, tables):
    """从 Contact / ChatRoom 等表建 wxid->显示名 映射。"""
    m = {}
    for t in tables:
        tl = t.lower()
        if tl not in ("contact", "chatroom", "bizinfo", "contactheadimgurl"):
            continue
        cols = [c[1] for c in conn.execute(f"PRAGMA table_info('{t}')").fetchall()]
        u = next((c for c in ("UserName", "userName", "wxid") if c in cols), None)
        n = next((c for c in ("NickName", "nickName", "Remark", "remark",
                              "DisplayName") if c in cols), None)
        if not (u and n):
            continue
        for r in conn.execute(f"SELECT {u},{n} FROM '{t}'"):
            if r[0] and r[1]:
                m[r[0]] = r[1]
    return m


def _ingest_one(path, msg_table):
    """导入单个含 MSG 表的 db 文件。返回新增条数。"""
    conn = _open(path)
    conn.row_factory = sqlite3.Row
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    cols = [c[1] for c in conn.execute(f"PRAGMA table_info('{msg_table}')").fetchall()]
    print(f"MSG 表={msg_table}, 字段={cols}")

    c_talker = next((c for c in ("strTalker", "talker", "userName", "UserName") if c in cols), "strTalker")
    c_content = next((c for c in ("strContent", "content", "strMsg") if c in cols), "strContent")
    c_ts = next((c for c in ("CreateTime", "createTime") if c in cols), "CreateTime")
    c_sender = next((c for c in ("IsSender", "isSender", "isSend") if c in cols), "IsSender")
    c_msgid = next((c for c in ("MsgSvrID", "msgSvrId", "msgId", "MsgId", "msgid") if c in cols), None)

    name_map = build_name_map(conn, tables)
    print(f"列映射: talker={c_talker} content={c_content} ts={c_ts} sender={c_sender} "
          f"msgid={c_msgid}; 联系人映射 {len(name_map)} 条")

    sel = f"SELECT {c_talker},{c_content},{c_ts},{c_sender}"
    if c_msgid:
        sel += f",{c_msgid}"
    sel += f" FROM '{msg_table}'"
    rows = conn.execute(sel).fetchall()

    n = 0
    for r in rows:
        talker, content, ts, sender = r[0], r[1], r[2], r[3]
        msgid = r[4] if c_msgid else None
        if not content:
            continue
        try:
            ts = int(ts)
        except Exception:
            ts = int(time.time())
        if ts > 1e12:          # 毫秒 → 秒
            ts = ts // 1000
        role = "me" if str(sender) in ("1", "1.0", "True", "true") else "them"
        # 我方发出的消息由 wechat_ui 键盘发送时实时写库，这里跳过，避免与微信库重复。
        if role == "me":
            continue
        conv_id = name_map.get(talker, talker)
        # 去重键优先用微信消息 ID（稳定）；没有则退化为 会话+时间+内容。
        dedupe = str(msgid) if msgid else None
        if add_message(conv_id, role, str(content), ts, raw=str(content),
                       name=conv_id, msg_key=dedupe):
            n += 1
    print(f"  本库新增 {n} 条（来源 {path}）")
    return n


def ingest(source):
    init()
    files = []
    if os.path.isfile(source):
        files = [source]
    else:
        for root, _, fs in os.walk(source):
            for f in fs:
                if f.lower().endswith(".db"):
                    files.append(os.path.join(root, f))
    total = 0
    for path in files:
        msg_table = _pick_msg_table(path)
        if not msg_table:
            continue
        total += _ingest_one(path, msg_table)
    print(f"导入完成：共新增 {total} 条消息")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", help="PyWxDump 解密后的 .db 文件或所在目录")
    ap.add_argument("--probe", action="store_true", help="仅打印表/字段结构，不导入")
    a = ap.parse_args()
    if not a.source:
        ap.print_help()
    elif a.probe:
        probe(a.source)
    else:
        ingest(a.source)
