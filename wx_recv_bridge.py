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

增量水印设计（支撑成千上万会话，不会每轮全量遍历）：
- 每会话在 monitor_store.poll_cursor 记 (last_time, max_seq) 水印；
- 首见会话取最近 50 条建基线（一次性）；之后只查 sort_seq > 水印的新消息
  （get_new_messages），空闲会话 0 行返回（亚毫秒级索引查询）。

媒体真实渲染（图片/动画表情/视频/文件/链接卡片）：
- 新消息遇媒体类型即下载落地到 wechat_media/（MediaDownloader；图片密钥链：
  cfgDword 派生 → 本地缓存（体检时已拿）→ 内存扫描，离线可用）；
- 链接/文件卡片用 get_message_row 取原始 XML 解析 title/descr/url，封面图 best-effort；
- 看板 /media/ 路由 + 气泡渲染 <img>/<video>/文件 chip/卡片；
- 历史占位消息补渲染：--media-backfill（幂等可重跑）。

依赖：pip install wechatauto-replica
看板：python dashboard.py 后访问 http://localhost:5000
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time

import monitor_store

try:
    from wechatauto import WeChatDB
except ImportError:
    print("[!] 需要 wechatauto-replica: pip install wechatauto-replica")
    sys.exit(1)

POLL_INTERVAL = 10
PER_SESSION_LIMIT = 50   # 首见会话的基线深度（最近 N 条）；之后走增量水印，与此无关
SESSION_LIMIT = 5000     # 每轮扫描的会话数上限（支撑成千上万联系人）

GROUP_SUFFIX = "@chatroom"

# 媒体落地目录（看板 /media/ 路由从这里提供文件）
MEDIA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wechat_media")
# 需要真实渲染的消息类型（type 是中文字符串）
MEDIA_TYPES = {"图片", "动画表情", "视频", "文件/链接/卡片"}
IMG_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")
VIDEO_EXTS = (".mp4", ".mov", ".webm")

_md = None  # MediaDownloader 懒初始化（首次遇到媒体消息时才建）


def _get_md(db):
    global _md
    if _md is None:
        try:
            from wechatauto import MediaDownloader
            _md = MediaDownloader(db, save_dir=MEDIA_DIR)
            print("[media] MediaDownloader 就绪，媒体目录: %s" % MEDIA_DIR)
        except Exception as e:
            print("[!] MediaDownloader 初始化失败（媒体只显示占位）: %s" % e)
            _md = False
    return _md or None


def _guess_ext(data):
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:4] == b"\x89PNG":
        return ".png"
    if data[:4] == b"GIF8":
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:4] == b"wxgf":
        return ".wxgf"   # 微信动画表情容器，浏览器不能直接播
    if data[4:8] == b"ftyp":
        return ".mp4"
    return ".bin"


def download_media(db, user, m):
    """下载一条媒体消息。返回 (相对路径 or None, 扩展名 or None)。
    download_media 可能返回路径（已落 save_dir）或字节，两种都兼容。"""
    md = _get_md(db)
    if not md:
        return None, None
    local_id = m.get("local_id")
    if not local_id:
        return None, None
    try:
        result = md.download_media(user, local_id)
    except Exception as e:
        print("[media] 下载失败 %s/%s: %s" % (user, local_id, e))
        return None, None
    if not result:
        return None, None
    # 形态一：文件路径
    p = str(result)
    if os.path.isfile(p):
        try:
            ap, base = os.path.abspath(p), os.path.abspath(MEDIA_DIR)
            if os.path.commonpath([ap, base]) == base:
                rel = os.path.relpath(ap, base)          # 已在媒体目录内
            else:
                dst_dir = os.path.join(MEDIA_DIR, "files")
                os.makedirs(dst_dir, exist_ok=True)
                dst = os.path.join(dst_dir, os.path.basename(p))
                shutil.copyfile(p, dst)
                rel = os.path.relpath(dst, base)
            return rel.replace("\\", "/"), os.path.splitext(p)[1].lower()
        except Exception as e:
            print("[media] 落地失败 %s: %s" % (p, e))
            return None, None
    # 形态二：字节
    if isinstance(result, (bytes, bytearray)):
        data = bytes(result)
        ext = _guess_ext(data)
        sub = os.path.join(MEDIA_DIR, "img")
        os.makedirs(sub, exist_ok=True)
        fn = os.path.join(sub, "%s_%s%s" % (user, local_id, ext))
        with open(fn, "wb") as f:
            f.write(data)
        return os.path.relpath(fn, MEDIA_DIR).replace("\\", "/"), ext
    return None, None


def _fmt_size(n):
    """字节数格式化为微信风格：5.2M / 130.0K / 812B。"""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    if n >= 1024 * 1024:
        return "%.1fM" % (n / 1048576)
    if n >= 1024:
        return "%.1fK" % (n / 1024)
    return "%dB" % n


# appmsg type -> 卡片种类（微信协议常量）
APPMSG_KIND = {"5": "link", "6": "file", "33": "app", "36": "app",
               "51": "app", "53": "app", "63": "app"}


def parse_card(db, user, m):
    """解析「文件/链接/卡片」消息的原始 XML（get_message_row 的 content 字段）。
    返回 {title, descr, url, thumb, ext, size, source, kind} 或 None。"""
    local_id = m.get("local_id")
    if not local_id:
        return None
    try:
        row = db.get_message_row(user, local_id)
    except Exception:
        return None
    if not row:
        return None
    content = row.get("content")
    if not content:
        return None
    if isinstance(content, bytes):
        content = content.decode("utf-8", "ignore")
    if "<appmsg" not in content and "<msg>" not in content:
        return None

    def _g(pat, flags=0):
        mm = re.search(pat, content, flags)
        return mm.group(1).strip() if mm else ""

    card = {
        "title": _g(r"<title>(.*?)</title>", re.S),
        "descr": _g(r"<descr>(.*?)</descr>", re.S),
        "url": _g(r"<url>(.*?)</url>"),
        "thumb": _g(r'cdnmidimg="([^"]+)"') or _g(r"<thumburl>(.*?)</thumburl>"),
        "ext": _g(r"<fileext>([^<]*)</fileext>"),
        "size": _fmt_size(_g(r"<totalsize>(\d+)</totalsize>")),
        # 来源名：sourcedisplayname（如「微信电脑版」）或 appinfo 的 appname（如「千问APP」）
        "source": (_g(r"<sourcedisplayname>(.*?)</sourcedisplayname>", re.S)
                   or _g(r'appname="([^"]+)"')),
        "kind": APPMSG_KIND.get(_g(r"<type>(\d+)</type>"), "link"),
    }
    if not card["descr"] and card["size"]:
        card["descr"] = card["size"]   # 文件消息副标题用大小（微信即如此）
    if not any([card["title"], card["descr"], card["url"]]):
        return None
    return card


def download_thumb(thumb):
    """下载链接卡片的封面图（CDN URL，best-effort，失败返回 None）。"""
    if not thumb:
        return None
    url = thumb
    if not url.startswith("http"):
        url = "https://mmsns.qpic.cn" + (url if url.startswith("/") else "/" + url)
    try:
        import requests
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200 and len(r.content) > 100:
            data = r.content
            ext = _guess_ext(data)
            sub = os.path.join(MEDIA_DIR, "thumbs")
            os.makedirs(sub, exist_ok=True)
            fn = os.path.join(sub, hashlib.md5(url.encode("utf-8")).hexdigest() + ext)
            with open(fn, "wb") as f:
                f.write(data)
            return os.path.relpath(fn, MEDIA_DIR).replace("\\", "/")
    except Exception:
        pass
    return None


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


def poll_new(db, progress=False):
    """轮询一轮：新入站消息写入 monitor_store，返回新增消息 dict 列表
    （conv_id/name/text/ts/type/role）。auto_reply.py 复用本函数实现自动回复闭环。

    增量水印设计（支撑成千上万会话，不会每轮全量遍历）：
    - 每会话在 monitor_store.poll_cursor 记 (last_time, max_seq) 水印；
    - 首见某会话：get_messages(limit=PER_SESSION_LIMIT) 取最近 N 条建基线（一次性）；
    - 之后：get_new_messages(user, since_seq=max_seq) 只取 sort_seq 大于水印的新消息，
      空闲会话返回 0 行（索引查询，亚毫秒级）——不再每轮拉 50 条全量。
    """
    new_items = []
    t0 = time.time()
    sessions = db.get_sessions(limit=SESSION_LIMIT)
    total = len(sessions)
    n_baseline = n_incr = 0
    get_new = getattr(db, "get_new_messages", None)  # 老版本库没有则回退全量
    for idx, s in enumerate(sessions, 1):
        user = s.get("username")
        if not user:
            continue
        try:
            name = db.get_nickname(user) or user
        except Exception:
            name = user
        cursor = monitor_store.get_cursor(user)
        try:
            if cursor and cursor.get("max_seq") and get_new:
                msgs = get_new(user, since_seq=cursor["max_seq"])
                n_incr += 1
            else:
                if progress:
                    print("[scan] %d/%d %s（首次建基线）" % (idx, total, name))
                msgs = db.get_messages(user, limit=PER_SESSION_LIMIT)
                n_baseline += 1
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
            # 媒体真实渲染：图片/表情/视频/文件 下载落地；链接卡片解析 XML
            media_rel, media_ext, card_json = None, None, None
            mtype = m.get("type")
            if mtype in MEDIA_TYPES:
                media_rel, media_ext = download_media(db, user, m)
                if mtype == "文件/链接/卡片":
                    card = parse_card(db, user, m)
                    if card:
                        if media_rel:
                            card["kind"] = "file"      # 真文件（已下载）
                        elif card.get("thumb"):
                            card["thumb"] = download_thumb(card.get("thumb"))
                        card_json = json.dumps(card, ensure_ascii=False)
                    elif media_rel:
                        text = os.path.basename(media_rel)   # XML 没解析出标题，用文件名兜底
                if media_rel:
                    if mtype == "图片":
                        text = "[图片]"
                    elif mtype == "动画表情":
                        text = "[动画表情]"
                    elif mtype == "视频":
                        text = "[视频]"
                    print("[media] + %s %s -> %s" % (name, mtype, media_rel))
            try:
                if monitor_store.add_message(
                    conv_id=user, role=role, text=text, ts=ts,
                    raw=raw, name=name, msg_key=msg_key_of(m, user),
                    media=media_rel, card=card_json,
                ):
                    new_items.append({
                        "conv_id": user, "name": name, "text": text,
                        "ts": ts, "type": m.get("type"), "role": role,
                    })
                    print("[store] + [%s][%s] %s"
                          % (name, "我" if self_msg else "对方", text[:60]))
            except Exception as e:
                print("[!] 写入失败 %s: %s" % (user, e))
        # 推进水印：max_seq 取本批最大值（不小于旧值）；last_time 用会话表的值
        max_seq = cursor["max_seq"] if cursor else 0
        for m in msgs:
            try:
                seq = int(m.get("sort_seq") or 0)
            except (TypeError, ValueError):
                seq = 0
            if seq > max_seq:
                max_seq = seq
        monitor_store.set_cursor(user, s.get("last_time") or 0, max_seq)
    if progress:
        print("[poll] 扫描 %d 会话（基线 %d / 增量 %d），耗时 %.1fs"
              % (total, n_baseline, n_incr, time.time() - t0))
    return new_items


def poll_once(db, progress=False):
    return len(poll_new(db, progress=progress))


def media_backfill(db, per_session=50):
    """历史媒体回填：对每会话最近 N 条里的媒体消息下载落地，并补更到已存记录
    （解决「先存了占位、后来才开媒体渲染」的历史消息）。幂等，可重复跑。"""
    sessions = db.get_sessions(limit=SESSION_LIMIT)
    total = len(sessions)
    updated = 0
    for idx, s in enumerate(sessions, 1):
        user = s.get("username")
        if not user:
            continue
        print("[backfill] %d/%d %s" % (idx, total, user))
        try:
            msgs = db.get_messages(user, limit=per_session)
        except Exception as e:
            print("[!] 读取失败 %s: %s" % (user, e))
            continue
        for m in msgs:
            mtype = m.get("type")
            if mtype not in MEDIA_TYPES:
                continue
            local_id = m.get("local_id")
            if not local_id:
                continue
            media_rel, _ext = download_media(db, user, m)
            card_json, text = None, None
            if mtype == "文件/链接/卡片":
                card = parse_card(db, user, m)
                if card:
                    if media_rel:
                        card["kind"] = "file"
                    elif card.get("thumb"):
                        card["thumb"] = download_thumb(card.get("thumb"))
                    card_json = json.dumps(card, ensure_ascii=False)
                elif media_rel:
                    text = os.path.basename(media_rel)
            if media_rel:
                text = {"图片": "[图片]", "动画表情": "[动画表情]",
                        "视频": "[视频]"}.get(mtype)
            if media_rel or card_json:
                n = monitor_store.update_media(
                    user, "%s|%s" % (user, local_id),
                    media=media_rel, card=card_json, text=text)
                updated += n
                if n:
                    print("[backfill] + %s %s -> %s" % (user, mtype, media_rel or "card"))
    print("[backfill] 完成，补更 %d 条历史消息" % updated)


def main():
    # 行缓冲：避免管道/部分终端环境下 print 被块缓冲憋到进程退出才输出
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="只轮询一轮后退出")
    ap.add_argument("--interval", type=int, default=POLL_INTERVAL, help="轮询间隔秒数")
    ap.add_argument("--media-backfill", action="store_true",
                    help="历史媒体回填：给已入库的占位消息补下载图片/视频/文件/卡片后退出")
    ap.add_argument("--depth", type=int, default=50, help="回填深度：每会话最近 N 条")
    args = ap.parse_args()

    monitor_store.init()
    print("[*] 库存统计: %s" % monitor_store.stats())
    try:
        db = WeChatDB()
        me = db.get_self_info()
        print("[*] 当前账号: %s (%s)" % (me.get("nick_name"), me.get("username")))
    except Exception as e:
        print("[!] WeChatDB 初始化失败（微信需已登录并跑过 wechatauto 体检）: %s" % e)
        sys.exit(1)

    if args.media_backfill:
        print("[*] 历史媒体回填模式（每会话最近 %d 条）..." % args.depth)
        media_backfill(db, per_session=args.depth)
        return

    print("[*] 收消息桥启动，每 %ds 轮询一次（Ctrl+C 停止）" % args.interval)
    print("[*] 首轮要全量解密全部数据库，可能数分钟；之后每轮秒级。进度见 [scan] 行")
    while True:
        try:
            n = poll_once(db, progress=True)
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
