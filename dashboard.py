#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
微信客户维护 —— 网页看板（仿微信：左会话列表 + 右会话详情）。
数据来自统一消息库 wechat_monitor.db（wx_recv_bridge 写入收件 + wechat_ui 写入发件）。

运行（在你本机，和微信同一台 Windows）：
    pip install -r requirements.txt
    python dashboard.py
然后浏览器打开 http://localhost:5000

API：
    GET  /api/conversations       会话列表（每项带 auto_reply 开关状态 + 全局 auto_reply_enabled/allow_groups）
    GET  /api/messages?conv=<id>  某会话消息
    POST /api/messages/delete    {id} 删除单条消息（脏数据；只删看板库，不动微信）
    POST /api/open  {conv}        标记已读（清未读）
    POST /api/send  {conv,text}   触发键盘发送（需微信在前台；会写回发送记录）
    POST /api/switch {conv,enabled}  设置某会话的AI自动回复开关（看板每会话一个，默认关）
    朋友圈轻互动（moment_ops）：
    GET  /api/moments             最近的新圈 + 未读互动统计 + 各客户自动互动开关
    POST /api/moment/action       点赞/取消/评论 {tid, action, text}（dry-run 保护+上限）
    POST /api/moment/ai-comment   AI 生成评论文案 {tid}
    POST /api/moment/switch       设置某客户自动点赞/评论开关 {username, nickname, field, enabled}（默认关）
    POST /api/moment/inter-read   互动通知全部标为已读
    GET  /api/tasks               维护任务队列状态（只读：各状态计数 + 最近 30 条）
    加好友（friend_add）：
    GET  /api/friends             开关状态 + 今日用量 + 各状态计数 + 最近 50 条
    POST /api/friends/toggle      {enabled} 开关自动加好友（落库，20 秒内生效，无需重启）
    POST /api/friends/import      导入名单：multipart 字段 file=CSV，或 JSON {text}
"""
import os
import io
import csv
import logging
import threading
from urllib.parse import quote

from flask import (Flask, render_template, jsonify, request, send_from_directory,
                   send_file, Response)

from monitor_store import (
    init, list_conversations, get_messages, add_message, clear_unread, set_switch,
    get_moments, get_interactions, mark_interactions_read, moment_unread_count,
    get_moment_switches, set_moment_switch, get_tasks, task_counts,
    friend_counts, get_friend_tasks, friend_sent_today, get_setting, set_setting,
    count_outreach_since, delete_message, get_avatar,
    list_contacts,
)
import outreach_core as oc

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("dashboard")

init()
BASE = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, template_folder=os.path.join(BASE, "templates"))
MEDIA_DIR = os.path.join(BASE, "wechat_media")
os.makedirs(MEDIA_DIR, exist_ok=True)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/media/<path:rel>")
def media_file(rel):
    """提供看板媒体文件（图片/视频/文件/卡片封面），防目录穿越。"""
    safe = os.path.normpath(rel).replace("\\", "/")
    if safe.startswith("..") or os.path.isabs(safe):
        return jsonify({"error": "bad path"}), 400
    return send_from_directory(MEDIA_DIR, safe)


@app.route("/api/avatar/<conv_id>")
def api_avatar(conv_id):
    """会话头像（联系人 small_head_url 下载到 wechat_media/avatars/ 后本地返回）。

    没有头像就 404 —— 前端回退成「名字首字」色块，不留破图。
    头像很少变，给一天缓存；换头像时前端 URL 带 v=ts，会自然失效。
    """
    row = get_avatar(conv_id)
    rel = (row or {}).get("file") or ""
    if not rel:
        return ("", 404)
    base = os.path.abspath(MEDIA_DIR)
    full = os.path.abspath(os.path.join(base, rel.replace("\\", "/")))
    if not full.startswith(base + os.sep) or not os.path.isfile(full):
        return ("", 404)
    resp = send_file(full, conditional=True)
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


def _conv_tags(conv, contacts):
    """给一个会话挂标签（来源：config.json 的 contacts，人工维护）。

    匹配规则与 auto_reply._contact_of 保持一致：wxid 精确 > 名字精确 > 名字包含
    （备注改过名时不至于掉标签）。返回去重后的标签列表，没有就返回 []。
    """
    cid = (conv.get("conv_id") or "").strip()
    nm = (conv.get("name") or "").strip()
    exact = loose = None
    for c in contacts or []:
        tags = [str(t).strip() for t in (c.get("tags") or []) if str(t).strip()]
        if not tags:
            continue
        cw = (c.get("wxid") or "").strip()
        cn = (c.get("name") or "").strip()
        if cw and cw == cid:
            return tags
        if exact is None and cn and cn == nm:
            exact = tags
        elif loose is None and len(cn) >= 2 and nm and cn in nm:
            loose = tags
    return exact or loose or []


@app.route("/api/conversations")
def api_conversations():
    # 带上自动回复全局状态：总开关(config.auto_reply.enabled)与群开关(allow_groups)，
    # 前端据此提示「为什么会话开关打开了却没反应」。
    import wechat_ui
    cfg = wechat_ui.load_config()
    ar = cfg.get("auto_reply", {}) or {}
    contacts = cfg.get("contacts") or []
    convs = list_conversations()
    for c in convs:                    # 看板「+ 选择托管」按标签筛选用
        c["tags"] = _conv_tags(c, contacts)
        # 头像：/api/avatar/<conv_id>?v=<ts>，ts 当缓存键；没有头像给空串，
        # 前端回退成「名字首字」色块（不留破图）
        c["avatar_url"] = (
            "/api/avatar/%s?v=%s" % (quote(c.get("conv_id") or "", safe=""),
                                     c.get("avatar_ts") or 0)
        ) if c.get("avatar") else ""
    return jsonify({
        "conversations": convs,
        "auto_reply_enabled": bool(ar.get("enabled", False)),
        "allow_groups": bool(ar.get("allow_groups", False)),
        "me": get_setting("me_wxid", "") or "",   # 本机 wxid：给自己的消息配头像
    })


@app.route("/api/messages")
def api_messages():
    conv = request.args.get("conv", "")
    return jsonify(get_messages(conv))


@app.route("/api/contacts")
def api_contacts():
    """通讯录全量（合并会话状态与托管开关），供「+」选择器使用。

    为什么不复用 /api/conversations：那张表只攒「聊过的会话」，加了好友却从没
    聊过的人根本不在里面——选择器永远选不到，也就没法开托管。
    数据来自 monitor_store.contacts（由 wx_recv_bridge.sync_contacts 从微信
    contact.db 同步），再看有没有对应会话、有没有开托管。
    """
    import wechat_ui
    cfg_contacts = wechat_ui.load_config().get("contacts") or []
    convs = {c["conv_id"]: c for c in list_conversations()}
    out = []
    for r in list_contacts():
        cid = r["conv_id"]
        cv = convs.get(cid) or {}
        item = {
            "conv_id": cid,
            "name": r.get("name") or r.get("nick") or cid,
            "kind": r.get("kind") or "friend",
            "has_conv": bool(cv),
            "last_msg": cv.get("last_msg") or "",
            "last_ts": cv.get("last_ts") or 0,
            "auto_reply": 1 if cv.get("auto_reply") else 0,
        }
        item["tags"] = _conv_tags(item, cfg_contacts)
        if cv.get("avatar_url"):
            item["avatar_url"] = cv["avatar_url"]
        else:
            av = get_avatar(cid)
            item["avatar_url"] = (
                "/api/avatar/%s?v=%s" % (quote(cid, safe=""), av.get("ts") or 0)
            ) if av else ""
        out.append(item)
    # 聊过的排前面（按最近活跃），没聊过的按名字兜底
    out.sort(key=lambda x: (0 if x["has_conv"] else 1, -(x["last_ts"] or 0), x["name"]))
    return jsonify({"contacts": out})


@app.route("/api/messages/delete", methods=["POST"])
def api_messages_delete():
    """删一条消息（入错库的脏数据）。只删看板库，不动微信客户端。"""
    data = request.get_json(force=True, silent=True) or {}
    mid = data.get("id")
    if mid in (None, ""):
        return jsonify({"ok": False, "error": "缺少 id"}), 400
    if not delete_message(mid):
        return jsonify({"ok": False, "error": "消息不存在或已删除"}), 404
    return jsonify({"ok": True})


@app.route("/api/open", methods=["POST"])
def api_open():
    data = request.get_json(force=True, silent=True) or {}
    conv = data.get("conv", "")
    if conv:
        clear_unread(conv)
    return jsonify({"ok": True})


@app.route("/api/switch", methods=["POST"])
def api_switch():
    """设置某会话的AI自动回复开关。body: {conv, enabled}。默认全关，只回打开的会话。"""
    data = request.get_json(force=True, silent=True) or {}
    conv = (data.get("conv") or "").strip()
    if not conv:
        return jsonify({"ok": False, "error": "conv 不能为空"}), 400
    enabled = bool(data.get("enabled"))
    set_switch(conv, enabled)
    return jsonify({"ok": True, "conv": conv, "enabled": enabled})


@app.route("/api/send", methods=["POST"])
def api_send():
    """通过键盘驱动把消息发给指定联系人（需微信在前台）。"""
    data = request.get_json(force=True, silent=True) or {}
    conv, text = data.get("conv", "").strip(), data.get("text", "").strip()
    if not conv or not text:
        return jsonify({"ok": False, "error": "conv 和 text 不能为空"}), 400
    try:
        import wechat_ui
        cfg = wechat_ui.load_config()
        cfg["dry_run"] = False
        bot = wechat_ui.WeChatPCBot(cfg)
        bot.send_to_contact(conv, text)   # 内部按键盘流程真发，并写回 store
        return jsonify({"ok": True, "conv": conv})
    except Exception as e:  # noqa: BLE001
        log.exception("发送失败")
        return jsonify({"ok": False, "error": str(e)}), 500


# ==================== 朋友圈轻互动 ====================

def _moment_ops(dry_run=None):
    """构造 MomentOps（看板与轮询共用）。import 失败时返回 None。"""
    try:
        import wechat_ui
        import moment_ops
        return moment_ops.MomentOps(wechat_ui.load_config(), dry_run=dry_run)
    except Exception as e:  # noqa: BLE001
        log.warning("MomentOps 不可用: %s", e)
        return None


@app.route("/api/moments")
def api_moments():
    """最近的新圈（50 条）+ 未读互动通知 + 各客户自动互动开关。"""
    ops = _moment_ops()
    return jsonify({
        "moments": get_moments(limit=50),
        "interactions": get_interactions(limit=50),
        "unread_inter": moment_unread_count(),
        "dry_run": bool(ops.dry_run) if ops else True,
        "max_actions": ops.max_actions if ops else 0,
        "switches": get_moment_switches(),
    })


@app.route("/api/moment/action", methods=["POST"])
def api_moment_action():
    """点赞/取消/评论。body: {tid, action: like|unlike|comment, text}。

    三道安全闸在 moment_ops 内部：dry-run（默认模拟）→ 每日上限
    （`max_actions_per_day`，0=不限）→ 库内置 rhythm 节流。
    放行标准 = 人工显式确认（这次点击本身）。
    """
    data = request.get_json(force=True, silent=True) or {}
    tid = str(data.get("tid") or "").strip()
    action = (data.get("action") or "").strip()
    text = (data.get("text") or "").strip()
    if not tid:
        return jsonify({"ok": False, "error": "tid 不能为空"}), 400
    ops = _moment_ops()
    if not ops:
        return jsonify({"ok": False, "error": "moment_ops 不可用（wechatauto 未装？）"}), 500
    try:
        if action == "like":
            r = ops.like(tid, cancel=False)
        elif action == "unlike":
            r = ops.like(tid, cancel=True)
        elif action == "comment":
            if not text:
                return jsonify({"ok": False, "error": "评论内容不能为空"}), 400
            r = ops.comment(tid, text)
        else:
            return jsonify({"ok": False, "error": "未知 action: %r" % action}), 400
        return jsonify(r)
    except Exception as e:  # noqa: BLE001
        log.exception("朋友圈动作失败")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/moment/ai-comment", methods=["POST"])
def api_moment_ai_comment():
    """AI 生成一条评论文案（复用销售人设 + 知识库）。body: {tid}。"""
    data = request.get_json(force=True, silent=True) or {}
    tid = str(data.get("tid") or "").strip()
    ops = _moment_ops()
    if not ops:
        return jsonify({"ok": False, "error": "moment_ops 不可用"}), 500
    try:
        return jsonify(ops.ai_comment_text(tid))
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/moment/switch", methods=["POST"])
def api_moment_switch():
    """设置某客户的朋友圈自动互动开关。body: {username, field: like|comment, enabled}。

    与 /api/switch（会话自动回复）同款语义：默认全关；开开关只对之后
    新到的圈生效，不回溯历史圈。评论不可逆，故独立开关、默认关。
    """
    data = request.get_json(force=True, silent=True) or {}
    username = (data.get("username") or "").strip()
    field = (data.get("field") or "").strip()
    nickname = (data.get("nickname") or "").strip()
    if not username or field not in ("like", "comment"):
        return jsonify({"ok": False,
                        "error": "username 不能为空，且 field 须为 like/comment"}), 400
    enabled = bool(data.get("enabled"))
    if field == "like":
        st = set_moment_switch(username, auto_like=enabled, nickname=nickname)
    else:
        st = set_moment_switch(username, auto_comment=enabled, nickname=nickname)
    return jsonify({"ok": True, "switch": st})


@app.route("/api/moment/inter-read", methods=["POST"])
def api_moment_inter_read():
    """互动通知全部标为已读。"""
    n = mark_interactions_read()
    return jsonify({"ok": True, "cleared": n})


# ==================== 维护任务队列（只读可观测） ====================

@app.route("/api/tasks")
def api_tasks():
    """auto_reply 维护任务队列的状态账本（只读）。

    队列跑在 auto_reply 进程的内存里，与看板是两个进程；落库到 reply_task 表
    才能让看板「看得见」待生成/待发送/失败/超时。不提供取消、插队等控制。
    """
    return jsonify({
        "counts": task_counts(),
        "tasks": get_tasks(limit=30),
    })


# ==================== 加好友（名单 / 开关 / 进度） ====================
# 开关为什么不写 config.json：auto_reply 只在启动时读配置，写了要重启才生效。
# 改成落 app_setting 表，auto_reply 每 20 秒读一次 → 看板上点「关」立刻停手。

def _af_cfg():
    """读 config.json 的 add_friend 节（只为拿默认值与展示用的上限）。"""
    try:
        import json
        with open(os.path.join(BASE, "config.json"), "r", encoding="utf-8") as f:
            return (json.load(f) or {}).get("add_friend", {}) or {}
    except Exception:
        return {}


@app.route("/api/friends")
def api_friends():
    af = _af_cfg()
    setting = get_setting("add_friend_enabled")
    # 没在库里设过 → 用 config 的默认值（首次打开看板时看到的就是配置里的状态）
    enabled = (setting == "1") if setting not in (None, "") else bool(af.get("enabled", False))
    return jsonify({
        "enabled": enabled,
        "daily_limit": int(af.get("daily_limit", 10)),
        "sent_today": friend_sent_today(),
        "counts": friend_counts(),
        "tasks": get_friend_tasks(limit=50),
    })


@app.route("/api/friends/toggle", methods=["POST"])
def api_friends_toggle():
    body = request.get_json(silent=True) or {}
    if "enabled" not in body:
        return jsonify({"ok": False, "error": "缺少 enabled"}), 400
    on = bool(body.get("enabled"))
    set_setting("add_friend_enabled", "1" if on else "0")
    return jsonify({"ok": True, "enabled": on})


# ==================== 主动触达（沉默客户唤醒） ====================
# 同加好友：开关落 app_setting，auto_reply 下一轮巡检（≤5 分钟）就生效，
# 不用重启。默认关闭，要开得先确认内容和节奏都合适。

def _ou_cfg():
    """读 config.json 的 outreach 节（拿默认值与展示用的参数）。"""
    try:
        import json
        with open(os.path.join(BASE, "config.json"), "r", encoding="utf-8") as f:
            return (json.load(f) or {}).get("outreach", {}) or {}
    except Exception:
        return {}


@app.route("/api/outreach")
def api_outreach():
    ou = _ou_cfg()
    setting = get_setting("outreach_enabled")
    enabled = (setting == "1") if setting not in (None, "") else bool(ou.get("enabled", False))
    import datetime
    d = datetime.datetime.now()
    day_start = datetime.datetime(d.year, d.month, d.day).timestamp()
    return jsonify({
        "enabled": enabled,
        "daily_limit": int(ou.get("daily_limit", 0) or 0),   # 0=不限
        "daily_unlimited": int(ou.get("daily_limit", 0) or 0) <= 0,
        "per_run_limit": int(ou.get("per_run_limit", 10)),
        "sent_today": count_outreach_since(day_start),
        "work_start_hour": int(ou.get("work_start_hour", 8)),
        "work_end_hour": int(ou.get("work_end_hour", 21)),
        "silent_days": int(ou.get("silent_days", 7)),
        "min_gap_days": int(ou.get("min_gap_days", 14)),
        "max_per_contact": int(ou.get("max_per_contact", 3)),
        "include_new_contacts": bool(ou.get("include_new_contacts", False)),
        # 只看不改：与常驻触达同一套规则算出来的候选（排查用，前端暂未展示）
        "candidates": oc.pick_targets(ou, limit=5),
    })


@app.route("/api/outreach/toggle", methods=["POST"])
def api_outreach_toggle():
    body = request.get_json(silent=True) or {}
    if "enabled" not in body:
        return jsonify({"ok": False, "error": "缺少 enabled"}), 400
    on = bool(body.get("enabled"))
    set_setting("outreach_enabled", "1" if on else "0")
    return jsonify({"ok": True, "enabled": on})


@app.route("/api/friends/import", methods=["POST"])
def api_friends_import():
    """上传 CSV（multipart 字段 file）或直接粘贴（JSON {text}）。"""
    try:
        import friend_add
    except Exception as e:      # noqa: BLE001
        return jsonify({"ok": False, "error": "friend_add 不可用: %s" % e}), 500
    text = ""
    f = request.files.get("file")
    if f is not None:
        raw = f.read()
        for enc in ("utf-8-sig", "utf-8", "gbk"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            return jsonify({"ok": False, "error": "编码无法识别（utf-8/gbk 都试了）"}), 400
    else:
        text = ((request.get_json(silent=True) or {}).get("text") or "")
    if not text.strip():
        return jsonify({"ok": False, "error": "没有内容"}), 400
    added, skipped = friend_add.import_text(text)
    return jsonify({"ok": True, "added": added,
                    "skipped": [{"keyword": k, "reason": r} for k, r in skipped]})


@app.route("/api/friends/template")
def api_friends_template():
    """下载名单 CSV 模板。

    与 friend_add.import_text 的解析规则严格对齐（表头 keyword,name,note,source）。
    输出带 UTF-8 BOM + CRLF：Excel 双击直接打开不乱码，另存回 CSV 也不用改编码。
    文件名走 ASCII，避免 Content-Disposition 里中文要额外做 RFC 5987 编码。
    """
    rows = [
        ["keyword", "name", "note", "source"],
        ["13800138000", "王工", "某设计院照明所所长，去年展会加的", "2026广州照明展"],
        ["wxid_abc123", "李总", "空间设计公司创始人，关注AI出图", "朋友介绍"],
    ]
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerows(rows)
    data = "\ufeff".encode("utf-8") + buf.getvalue().encode("utf-8")
    return Response(
        data, mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="friend_list_template.csv"'},
    )


def _avatar_loop():
    """后台补头像：启动先同步一次，之后每 10 分钟一次。

    头像来自微信 contact 表的 small_head_url（wx.qlogo.cn，无需登录态），
    新好友 / 换头像都能跟上。微信没开或读库失败只记一行日志——
    头像只是显示，不该影响看板本身。
    """
    import time as _t
    while True:
        try:
            from wx_recv_bridge import WeChatDB, sync_avatars, sync_contacts
            db = WeChatDB()
            sync_avatars(db)
            sync_contacts(db)      # 通讯录跟着一起刷新：新加的好友选择器里马上能看到
        except Exception as e:      # noqa: BLE001
            log.info("头像/通讯录同步跳过（微信未运行/读库失败）: %s", e)
        _t.sleep(600)


if __name__ == "__main__":
    # 用 WSGI（gunicorn/waitress）启动时不会走这里，需要自己起一个 _avatar_loop 线程
    threading.Thread(target=_avatar_loop, daemon=True, name="avatar-sync").start()
    port = int(os.environ.get("WECHAT_DASHBOARD_PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
