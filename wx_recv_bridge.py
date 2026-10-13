#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wx_recv_bridge.py —— 收消息桥：wechatauto-replica 数据库解密路线 -> monitor_store -> dashboard

路线说明（2026-10-13 体检实测）：
- 读消息走 WeChatDB（本地 SQLCipher 解密，只读，对微信版本不敏感），体检 20/20 库可解密；
- 与控件树/UIA 无关，新版微信自绘渲染不影响本脚本；
- 发消息不在本脚本职责内（继续用 wechat_ui.py 键盘驱动，已验证可用）。

用法：
    python wx_recv_bridge.py              # 轮询模式（默认 10s 一轮，Ctrl+C 停止）
    python wx_recv_bridge.py --once       # 只跑一轮（调试用）
    python wx_recv_bridge.py --interval 5 # 自定义轮询间隔

消息字段事实（读 wechatauto 源码确认，勿再凭老微信经验猜）：
- get_messages 返回 dict：local_id / type / sender_id / sender_username /
  create_time / content / sort_seq；**不含 server_id**（只有 get_message_row 有）。
- type 是**中文字符串**（"文本"/"图片"/"语音"/"视频"/"动画表情"/"位置"/
  "文件/链接/卡片"/"系统消息"），不是老微信的数字 type。
- content 库已处理好：文本=正文原文；媒体=[图片 md5=...] 或 [类型] 占位
  （长文本 zstd 压缩内容需 zstandard 库，装 wechatauto-replica 时会带上）。
- sender_id == 2 表示自己发的：**程序键盘发的**靠 sent_log 去重跳过（send_to_contact
  已即时写过库，防双渲染）；**用户在 PC 微信手动发的**不在 sent_log，正常补录上屏（role=me）。
- sender_username 是发送者 wxid（群消息可反查昵称，标注为「昵称: 内容」）。

依赖：pip install wechatauto-replica
看板：python dashboard.py 后访问 http://localhost:5000
"""
import argparse
import re
import sys
import time

import monitor_store

try:
    from wechatauto import WeChatDB
except ImportError:
    print("[!] 需要 wechatauto-replica: pip install wechatauto-replica")
    sys.exit(1)

POLL_INTERVAL = 10
PER_SESSION_LIMIT = 50   # 每会话每次取最近 N 条（去重靠 msg_key，重复取无副作用）
SESSION_LIMIT = 100

GROUP_SUFFIX = "@chatroom"


def _xml_placeholder(xml):
    """微信 4.x 部分消息（图片/表情等）的 content 是 XML 原文、库未友好化时，解析成占位。"""
    if "<img " in xml or "<img>" in xml:
        md5 = re.search(r'<md5="([0-9a-fA-F]{16,})"', xml)
        return "[图片%s]" % (" md5=%s" % md5.group(1) if md5 else "")
    if "<emoji" in xml or "gameext" in xml:
        return "[动画表情]"
    if "<videomsg" in xml:
        return "[视频]"
    if "<voicemsg" in xml:
        return "[语音]"
    if "<loc " in xml:
        return "[位置]"
    if "<appmsg" in xml or "<appmessage" in xml:
        title = re.search(r"<title>(.*?)</title>", xml, re.S)
        return "[链接/卡片%s]" % (" %s" % title.group(1).strip() if title else "")
    if "<sysmsg" in xml or "<revoke" in xml:
        return "[系统消息]"
    return "[特殊消息]"


def msg_text(m):
    """取消息显示文本。
    wechatauto 的 content 多数已处理好（文本=原文，媒体=占位），type 是中文字符串；
    但 4.x 部分消息类型库未覆盖，content 会原样透出 XML，这里做最后一道兜底。"""
    content = m.get("content")
    text = None
    if isinstance(content, str) and content.strip():
        text = content.strip()
    elif isinstance(content, bytes):
        try:
            text = content.decode("utf-8", "ignore").strip()
        except Exception:
            text = None
    if not text:
        t = m.get("type")
        return "[%s]" % t if t else ""
    if text.startswith("<?xml") or text.startswith("<msg") or text.startswith("<sysmsg"):
        return _xml_placeholder(text)
    return text


def msg_key_of(m, user):
    """每条消息一个稳定 key，供 monitor_store 去重。
    get_messages 的 dict 不含 server_id，local_id 在会话内唯一，优先用它。"""
    for k in ("local_id", "server_id"):
        v = m.get(k)
        if v:
            return "%s|%s" % (user, v)
    return "%s|%s|%s|%s" % (user, m.get("create_time"), m.get("sender_id"), m.get("content"))


def norm_ts(ts):
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return int(time.time())
    if ts > 10 ** 12:  # 毫秒 -> 秒
        ts //= 1000
    return ts


def is_self(m):
    """sender_id == 2 表示自己发的（与我方键盘发送记录重复，跳过）。"""
    sid = m.get("sender_id")
    return sid == 2 or sid == "2"


def poll_new(db):
    """轮询一轮：新入站消息写入 monitor_store，返回新增消息 dict 列表
    （conv_id/name/text/ts/type）。auto_reply.py 复用本函数实现自动回复闭环。"""
    new_items = []
    sessions = db.get_sessions(limit=SESSION_LIMIT)
    for s in sessions:
        user = s.get("username")
        if not user:
            continue
        try:
            name = db.get_nickname(user) or user
        except Exception:
            name = user
        try:
            msgs = db.get_messages(user, limit=PER_SESSION_LIMIT)
        except Exception as e:
            print("[!] 读取会话失败 %s: %s" % (user, e))
            continue
        for m in msgs:
            text = msg_text(m)
            if not text:
                continue
            ts = norm_ts(m.get("create_time"))
            self_msg = is_self(m)
            role = "me" if self_msg else "them"
            # 自己发的消息分两种：
            #   程序键盘发的 —— sent_log 命中，跳过（send_to_contact 已即时写过库，防双渲染）
            #   用户在 PC 微信手动发的 —— 不在 sent_log，正常补录，看板也要能看到
            if self_msg and monitor_store.was_sent(user, text):
                continue
            sender = m.get("sender_username") or ""
            if sender and not self_msg and user.endswith(GROUP_SUFFIX):
                try:
                    sender_name = db.get_nickname(sender) or sender
                except Exception:
                    sender_name = sender
                text = "%s: %s" % (sender_name, text)
            raw = str({k: m.get(k) for k in
                       ("type", "sender_id", "sender_username", "local_id")})
            try:
                if monitor_store.add_message(
                    conv_id=user, role=role, text=text, ts=ts,
                    raw=raw, name=name, msg_key=msg_key_of(m, user),
                ):
                    new_items.append({
                        "conv_id": user, "name": name, "text": text,
                        "ts": ts, "type": m.get("type"), "role": role,
                    })
                    print("[store] + [%s][%s] %s"
                          % (name, "我" if self_msg else "对方", text[:60]))
            except Exception as e:
                print("[!] 写入失败 %s: %s" % (user, e))
    return new_items


def poll_once(db):
    return len(poll_new(db))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="只轮询一轮后退出")
    ap.add_argument("--interval", type=int, default=POLL_INTERVAL, help="轮询间隔秒数")
    args = ap.parse_args()

    monitor_store.init()
    try:
        db = WeChatDB()
        me = db.get_self_info()
        print("[*] 当前账号: %s (%s)" % (me.get("nick_name"), me.get("username")))
    except Exception as e:
        print("[!] WeChatDB 初始化失败（微信需已登录并跑过 wechatauto 体检）: %s" % e)
        sys.exit(1)

    print("[*] 收消息桥启动，每 %ds 轮询一次（Ctrl+C 停止）" % args.interval)
    while True:
        try:
            n = poll_once(db)
            if n:
                print("[*] 本轮新增 %d 条 -> 看板 http://localhost:5000" % n)
        except KeyboardInterrupt:
            print("[*] 已停止")
            break
        except Exception as e:
            print("[!] 轮询异常: %s" % e)
        if args.once:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
