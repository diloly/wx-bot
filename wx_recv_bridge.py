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
import glob
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

# 群成员表缓存：chatroom -> (拉取时间, {wxid: 显示名})。备注优先于昵称
# （「某总」比「本名」更可能是客户对他的称呼）。TTL 5 分钟：群成员变动不频繁，
# 不值得每条消息都全量拉一次。
_GROUP_MEMBERS_TTL = 300
_group_members_cache = {}


def _group_member_name(db, chatroom, sender_wxid):
    """群消息发送者的显示名：优先微信群备注，退回昵称，再退回 wxid。"""
    if not sender_wxid:
        return ""
    now = time.time()
    hit = _group_members_cache.get(chatroom)
    if hit and now - hit[0] < _GROUP_MEMBERS_TTL:
        name = hit[1].get(sender_wxid)
        if name:
            return name
    else:
        try:
            members = db.get_group_members(chatroom) or []
        except Exception:
            members = []
        table = {}
        for m in members:
            wxid = m.get("username") or ""
            disp = m.get("remark") or m.get("nick_name") or ""
            if wxid and disp:
                table[wxid] = disp
        _group_members_cache[chatroom] = (now, table)
        name = table.get(sender_wxid)
        if name:
            return name
    # 缓存未命中（新成员/刚换备注）：单条反查兜底
    try:
        return db.get_nickname(sender_wxid) or sender_wxid
    except Exception:
        return sender_wxid


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


def _land_media_path(p):
    """把库返回的文件路径落到媒体目录内，返回相对 MEDIA_DIR 的路径。
    已在媒体目录内则原样相对化；否则复制进 files/。
    注意不能用 os.path.commonpath：源文件在 C: 盘（微信缓存）而项目在 E: 盘时
    它会抛 "Paths don't have the same drive"，跨盘必须走复制分支。"""
    try:
        ap = os.path.abspath(p)
        base = os.path.abspath(MEDIA_DIR)
        if ap.startswith(base + os.sep):
            rel = os.path.relpath(ap, base)              # 已在媒体目录内
        else:
            dst_dir = os.path.join(MEDIA_DIR, "files")
            os.makedirs(dst_dir, exist_ok=True)
            dst = os.path.join(dst_dir, os.path.basename(p))
            if os.path.abspath(dst) != ap:
                shutil.copyfile(p, dst)
            rel = os.path.relpath(dst, base)
        return rel.replace("\\", "/")
    except Exception as e:
        print("[media] 落地失败 %s: %s" % (p, e))
        return None


def download_media(db, user, m, original=False, scroll=True, chat_name=None):
    """下载一条媒体消息。返回 (相对路径 or None, 扩展名 or None)。
    download_media 可能返回路径（已落 save_dir）或字节，两种都兼容。

    original=True：图片强制走 download_image_original（要 _h.dat 真原件，
    本机没有会**驱动微信界面**点开气泡下载，可能滚动消息列表）——只允许
    在用户主动触发的 --media-backfill --original 里用，轮询路径绝不调用。"""
    md = _get_md(db)
    if not md:
        return None, None
    local_id = m.get("local_id")
    if not local_id:
        return None, None
    if original and m.get("type") == "图片":
        # 原图优先：先拿本地最高档，再驱动界面要 _h.dat 原件
        try:
            p = md.download_image_original(
                user, local_id, chat_name=chat_name, scroll=scroll)
            if p:
                return _land_media_path(p), os.path.splitext(str(p))[1].lower()
            print("[media] 原图未拿到 %s/%s，回退普通下载" % (user, local_id))
        except Exception as e:
            print("[media] 原图下载异常 %s/%s: %s（回退普通下载）" % (user, local_id, e))
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
        return _land_media_path(p), os.path.splitext(p)[1].lower()
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


def _xml_text(raw):
    """XML 取值后处理：剥 CDATA 包裹 + 反转义实体（&amp;/&lt; 等）。
    微信 appmsg 大量字段写成 <title><![CDATA[xx]]></title>，
    正则直接抓会把 CDATA 符号一起带进来；url 里的 &amp; 不反转义链接会坏。"""
    if not raw:
        return ""
    t = raw.strip()
    m = re.fullmatch(r"<!\[CDATA\[(.*?)\]\]>", t, re.S)
    if m:
        t = m.group(1).strip()
    for a, b in (("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'),
                 ("&apos;", "'"), ("&amp;", "&")):   # amp 放最后，防双重解
        t = t.replace(a, b)
    return t.strip()


def _decode_content(data, mtype=None):
    """把原始 message_content blob 解成 XML 文本。

    事实（2026-10-14 实测 wechatauto-replica 1.2.6.1）：
    - get_messages() 的 content 已过 _friendly_content，是干净的 XML 字符串 —— **首选**；
    - get_message_row() 的 content 是**未解密/未解压**的原始列（zstd 容器头
      0x28 0xb5 0x2f 0xfd），文档明说「媒体下载用」，不能直接当文本解析。
    只有真的拿到 bytes 时才走这里自己解。"""
    try:
        text = data.decode("utf-8")
        if text.strip():
            return text
    except UnicodeDecodeError:
        pass
    if data[:4] == b"\x28\xb5\x2f\xfd":
        try:
            import zstandard as _zstd
            text = _zstd.ZstdDecompressor().decompress(data).decode("utf-8", "ignore")
            if text.strip():
                return text
        except Exception:
            pass
    return None


# refermsg 里的被引用消息类型码 -> 看板摘要标签（1=文本走原文，49=文件/链接取标题）
QUOTE_TYPE_LABEL = {"3": "[图片]", "34": "[语音]", "43": "[视频]",
                    "47": "[动画表情]", "48": "[位置]", "50": "[音视频通话]"}


def _clean_quote_text(raw, qtype=""):
    """refermsg 的 content -> 引用块里显示的摘要。
    type 1(文本) 的 content 是纯文本，直接显示；其余类型 content 是转义后的 XML，
    有类型码时按类型码归一（比猜 XML 稳）；文件/链接类尽量把标题取出来，
    比干巴巴的 [消息] 有用得多。"""
    if not raw:
        return ""
    t = _xml_text(raw)
    if qtype in QUOTE_TYPE_LABEL:
        return QUOTE_TYPE_LABEL[qtype]
    if t.startswith("<?xml") or t.startswith("<msg"):
        if "<img " in t or "<img>" in t:
            return "[图片]"
        if "<videomsg" in t:
            return "[视频]"
        if "<voicemsg" in t:
            return "[语音]"
        if "<emoji" in t:
            return "[动画表情]"
        m = re.search(r"<title>(.*?)</title>", t, re.S)
        if m:
            title = _xml_text(m.group(1))
            if title:
                return "[文件/链接] " + title[:60]
        return "[消息]"
    return t[:120]


def parse_card(db, user, m):
    """解析「文件/链接/卡片」消息的 XML。
    主路径用 get_messages 已友好化的 content（str，干净 appmsg XML）；
    仅当拿到原始 blob(bytes) 时才自己解 zstd。db/user 保留作兜底扩展。
    返回 {title, descr, url, thumb, ext, size, source, kind, ...} 或 None。
    kind: file / link / app / quote(引用回复) / finder(视频号)。"""
    content = m.get("content")
    if isinstance(content, (bytes, bytearray)):
        content = _decode_content(bytes(content), m.get("type"))
    if not content or not isinstance(content, str):
        return None
    if "<appmsg" not in content and "<msg>" not in content:
        return None

    def _g(pat, flags=0):
        mm = re.search(pat, content, flags)
        return _xml_text(mm.group(1)) if mm else ""

    card = {
        "title": _g(r"<title>(.*?)</title>", re.S),
        # 微信 appmsg 副标题两种写法都有：<des>（4.x 常见）/ <descr>（老格式）
        "descr": _g(r"<descr>(.*?)</descr>", re.S) or _g(r"<des>(.*?)</des>", re.S),
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

    # 视频号分享（finderFeed）：封面/播放/头像/认证图标/频道名/视频地址
    if "<finderFeed>" in content:
        ff = {
            "nickname": _g(r"<nickname><!\[CDATA\[(.*?)\]\]></nickname>", re.S),
            "desc": _g(r"<desc><!\[CDATA\[(.*?)\]\]></desc>", re.S),
            "avatar": _g(r"<avatar><!\[CDATA\[(.*?)\]\]></avatar>"),
            "auth_icon": _g(r"<authIconUrl><!\[CDATA\[(.*?)\]\]></authIconUrl>"),
            "cover": (_g(r"<thumbUrl><!\[CDATA\[(.*?)\]\]></thumbUrl>")
                      or _g(r"<coverUrl><!\[CDATA\[(.*?)\]\]></coverUrl>")),
            "video": _g(r"<url><!\[CDATA\[(.*?)\]\]></url>"),
        }
        card["kind"] = "finder"
        card["finder"] = ff
        if not card["descr"] and ff["desc"]:
            card["descr"] = ff["desc"]
        return card

    # 引用回复（refermsg, appmsg type 57）：引用块 + 回复正文
    if "<refermsg>" in content:
        rm = re.search(r"<refermsg>.*?</refermsg>", content, re.S)
        rm_xml = rm.group(0) if rm else content
        # refermsg 内的 <type> 排在 <msgsource> 之后，取最后一个最稳
        # （msgsource 是转义 XML，理论上可能含干扰标签）
        types = re.findall(r"<type>(\d+)</type>", rm_xml)
        qtype = types[-1] if types else ""
        qname = _g(r"<displayname>(.*?)</displayname>", re.S)
        fromusr = _g(r"<fromusr>(.*?)</fromusr>")
        if not qname and fromusr:
            # displayname 偶尔为空（自己引自己/早期消息），用 wxid 反查昵称兜底
            try:
                qname = db.get_nickname(fromusr) or fromusr
            except Exception:
                qname = fromusr
        card["kind"] = "quote"
        card["quote"] = {
            "name": qname,
            "text": _clean_quote_text(
                _g(r"<refermsg>.*?<content>(.*?)</content>", re.S), qtype),
        }
        return card

    if not any([card["title"], card["descr"], card["url"]]):
        return None
    return card


def finalize_card(card, media_rel):
    """卡片后处理：文件 kind 覆写 + 视频号资源（封面/头像/认证图标）下载。
    下载失败时保留原值，不用 None 覆盖——否则断网重跑回填会把已下载的封面冲掉。"""
    if not card:
        return None
    if media_rel:
        card["kind"] = "file"
        return card
    f = card.get("finder")
    if f:
        for k in ("cover", "avatar", "auth_icon"):
            if f.get(k):
                got = download_url(f[k], "finder")
                if got:
                    f[k] = got
    elif card.get("thumb"):
        got = download_thumb(card.get("thumb"))
        if got:
            card["thumb"] = got
    return card


def download_thumb(thumb):
    """下载链接卡片的封面图（CDN URL，best-effort，失败返回 None）。"""
    return download_url(thumb, "thumbs")


def download_url(url, subdir="misc"):
    """通用 URL 下载（卡片封面/视频号头像/封面/认证图标），落地 MEDIA_DIR/<subdir>/。
    先查本地缓存（按 url 的 md5 命名），命中直接返回，不发网络请求——
    这样断网时重跑回填也不会把已下载好的封面路径冲成 None。"""
    if not url or not url.startswith("http"):
        return None
    d = os.path.join(MEDIA_DIR, subdir)
    os.makedirs(d, exist_ok=True)
    prefix = hashlib.md5(url.encode("utf-8")).hexdigest()
    for hit in sorted(glob.glob(os.path.join(d, prefix + ".*"))):
        if os.path.isfile(hit):
            return os.path.relpath(hit, MEDIA_DIR).replace("\\", "/")
    try:
        import requests
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200 and len(r.content) > 100:
            data = r.content
            ext = _guess_ext(data)
            fn = os.path.join(d, prefix + ext)
            if not os.path.exists(fn):
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
        t = _xml_text(title.group(1)) if title else ""
        return "[链接/卡片%s]" % (" %s" % t if t else "")
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

    **必须带 create_time**：local_id 只是「会话内自增」，不是全局唯一。
    删好友后重新加、或清空会话时，微信会**重建该会话**，local_id 从 1
    重新数（2026-10-19 实测：删好友重加后库里 local_id 变成 1/2/3/4，
    而 9 月那次会话早已存过同 key 的旧记录 → 哈希唯一索引冲突 →
    INSERT OR IGNORE 静默丢弃，客户发的「你是谁」「hello?」全都没入库，
    AI 自然无从触发）。带上 create_time 后，同 local_id 不同时间的消息
    是两个不同的 key，重建会话不再互相顶掉。

    create_time 是消息固有属性、跨轮询稳定，所以去重仍然有效；
    万一缺失就退回不含时间的老形态（宁可偶尔重复，不能永久丢消息）。
    """
    ct = m.get("create_time")
    ct = "" if ct in (None, "", 0, "0") else str(ct)
    for k in ("local_id", "server_id"):
        v = m.get(k)
        if v:
            return "%s|%s|%s" % (user, v, ct) if ct else "%s|%s" % (user, v)
    return "%s|%s|%s|%s" % (user, ct, m.get("sender_id"), m.get("content"))


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


# ---- 会话显示名同步（人工改过备注后，看板要跟着变）----
# conversations.name 原来只在「该会话有新消息」时由 add_message(name=...) 更新。
# 用户在微信里改备注/昵称**不会产生任何消息**，于是看板永远显示改名前的旧名字
# （2026-10-21 用户反馈：把「A站-胡梽彬」改成「A站-空间设计协同平台」，面板不动）。
# poll_new 每一轮本来就在逐个读 get_nickname，顺手缓存下来，每 60 秒批量比对一次
# 写回库——不额外查 contact.db，开销只有几十次字符串比较。
_NAME_SYNC_EVERY = 60.0
_name_cache = {}            # conv_id -> 本轮读到的备注/昵称
_last_name_sync = 0.0


def _sync_names():
    """把本轮读到的最新昵称写回 conversations（只写有变化的）。"""
    global _last_name_sync
    now = time.time()
    if now - _last_name_sync < _NAME_SYNC_EVERY:
        return
    _last_name_sync = now
    try:
        cur = {c["conv_id"]: (c.get("name") or "")
               for c in monitor_store.list_conversations()}
    except Exception:               # noqa: BLE001
        return
    changed = 0
    for cid, nm in list(_name_cache.items()):
        if not nm or cid not in cur or nm == cur[cid]:
            continue
        try:
            monitor_store.set_conversation_name(cid, nm)
            changed += 1
        except Exception:           # noqa: BLE001
            pass
    if changed:
        print("[bridge] 已同步 %d 个会话的备注/昵称" % changed)


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
        _name_cache[user] = name        # 供 _sync_names 定期回写（改备注后看板要跟着变）
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
            # 群聊发送者：文本消息拼进 text 前缀；但卡片消息的 text 会被 card 覆盖，
            # 前缀必然丢失，故卡片改存 card["sender"]，由前端在卡片上方渲染名字
            sender_name = ""
            sender = m.get("sender_username") or ""
            if sender and not self_msg and user.endswith(GROUP_SUFFIX):
                try:
                    sender_name = _group_member_name(db, user, sender)
                except Exception:
                    sender_name = sender
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
                        card = finalize_card(card, media_rel)
                        if sender_name:
                            card["sender"] = sender_name   # 群聊卡片的真实发送者
                        # 引用回复(type57)/视频号(type51) 给会话列表一个干净的预览文本
                        if card.get("kind") == "finder":
                            f = card.get("finder") or {}
                            text = "[视频号] " + (f.get("nickname") or "视频分享")
                        elif card.get("kind") == "quote":
                            text = card.get("title") or text
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
            # 群聊消息补发送者前缀（卡片已由 card.sender 承载，不重复拼）
            if sender_name and not card_json:
                text = "%s: %s" % (sender_name, text)
            try:
                if monitor_store.add_message(
                    conv_id=user, role=role, text=text, ts=ts,
                    raw=raw, name=name, msg_key=msg_key_of(m, user),
                    media=media_rel, card=card_json, dedupe_loose=True,
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
    _sync_names()      # 低频：把改过的备注/昵称同步回看板会话名
    if progress:
        print("[poll] 扫描 %d 会话（基线 %d / 增量 %d），耗时 %.1fs"
              % (total, n_baseline, n_incr, time.time() - t0))
    return new_items


def poll_once(db, progress=False):
    return len(poll_new(db, progress=progress))


def media_backfill(db, per_session=50, original=False, scroll=True):
    """历史媒体回填：对每会话最近 N 条里的媒体消息下载落地，并补更到已存记录
    （解决「先存了占位、后来才开媒体渲染」的历史消息）。幂等，可重复跑。

    original=True：图片强制要 _h.dat 真原件——会驱动微信界面（点气泡/滚动列表），
    仅限用户主动运行时开启，且运行时不能操作电脑。"""
    sessions = db.get_sessions(limit=SESSION_LIMIT)
    total = len(sessions)
    updated = 0
    n_original = 0
    for idx, s in enumerate(sessions, 1):
        user = s.get("username")
        if not user:
            continue
        print("[backfill] %d/%d %s" % (idx, total, user))
        is_group = user.endswith(GROUP_SUFFIX)
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
            media_rel, _ext = download_media(
                db, user, m, original=original, scroll=scroll,
                chat_name=db.get_nickname(user) or user)
            if original and mtype == "图片":
                n_original += 1
            card_json, text = None, None
            if mtype == "文件/链接/卡片":
                card = parse_card(db, user, m)
                if card:
                    card = finalize_card(card, media_rel)
                    # 群聊卡片补真实发送者（与 poll_new 一致）
                    if is_group and m.get("sender_username") and not is_self(m):
                        try:
                            sn = _group_member_name(db, user, m["sender_username"])
                        except Exception:
                            sn = m["sender_username"]
                        card["sender"] = sn
                    if card.get("kind") == "finder":
                        f = card.get("finder") or {}
                        text = "[视频号] " + (f.get("nickname") or "视频分享")
                    elif card.get("kind") == "quote":
                        text = card.get("title") or text
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
    if original:
        print("[backfill] 原图模式：处理图片 %d 张（会驱动微信界面，请勿操作电脑）" % n_original)
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
    ap.add_argument("--original", action="store_true",
                    help="（仅配合 --media-backfill）图片强制下载 _h.dat 真原件："
                         "会驱动微信界面点开气泡、滚动消息列表，运行时请勿操作电脑")
    ap.add_argument("--no-scroll-original", action="store_true",
                    help="配合 --original：只在当前屏幕可见区找原图，不滚动消息列表"
                         "（找不到就跳过，绝不打扰界面）")
    ap.add_argument("--reset-cursor", metavar="CONV_ID",
                    help="重置某会话的轮询水印，下一轮会重建基线（重读最近 50 条，"
                         "已入库的按宽松判重跳过）。传 all 重置全部。"
                         "用于「微信重建了会话（删好友重加/清空），新消息入不了库」")
    args = ap.parse_args()

    monitor_store.init()
    print("[*] 库存统计: %s" % monitor_store.stats())
    if args.reset_cursor:
        target = None if args.reset_cursor.lower() == "all" else args.reset_cursor
        n = monitor_store.reset_cursor(target)
        print("[*] 已重置 %d 个会话的水印（%s）；下一次轮询会重建基线"
              % (n, "全部" if target is None else target))
        return
    try:
        db = WeChatDB()
        me = db.get_self_info()
        print("[*] 当前账号: %s (%s)" % (me.get("nick_name"), me.get("username")))
    except Exception as e:
        print("[!] WeChatDB 初始化失败（微信需已登录并跑过 wechatauto 体检）: %s" % e)
        sys.exit(1)

    if args.media_backfill:
        print("[*] 历史媒体回填模式（每会话最近 %d 条）..." % args.depth)
        media_backfill(db, per_session=args.depth,
                       original=args.original,
                       scroll=not args.no_scroll_original)
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
