#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""人工接管（handoff）：客户表现出意向 → 通知真人 → 交出会话。

业务链路
--------
主动激活/触达发出去之后，客户回的话里如果出现「想了解」「怎么报价」「能不能试用」
这类信号，机器人继续自动聊容易把线索聊丢。这时应该：

    客户意向消息 → 给配置好的**群聊 + 私聊（真人）**各发一条内部通知
                 → **先不关托管**：等待期机器人继续接住客户，别把人晾着
                 → 真人在该客户会话里真的回了一句话，才正式关掉 AI 托管
                 → 超时仍没人接手，则解除待接管、恢复常规自动回复并留记录
                 → 本次不生成 AI 回复，避免抢真人的话
                 → 落一条 handoff_log，同一会话冷却期内不重复通知

为什么要「等真人回复才交出」：以前通知还没投出去就先 set_switch(False)，
collect() 里开关一关就把客户后续消息全部丢掉，中间那段空窗没人应答；
通知一个都没送出去时更糟——本次放行照常回，下一条照样全丢。

为什么要单独一个模块
--------------------
配置既有 config.json 的默认值，也有看板页面实时改的值（存 app_setting）；
判定既有 AI 二分类也有关键词兜底。集中在这里，auto_reply / dashboard 各取所需，
不会出现「看板改了但主进程没生效」这种两边各读一份的漂移。

合规边界：只对**已经被我们主动激活过的已知客户**做意向判定（watch_scope=activated），
不是对所有聊天内容做扫描；通知只发给用户自己配置的内部群/同事，不外发。
"""
import datetime
import json
import time

import ai_client
import monitor_store

SETTING_KEY = "handoff_config"

DEFAULTS = {
    "enabled": True,
    "targets": [],                 # [{conv_id, name, kind}] 或 ["群名", "同事名"]
    "watch_scope": "activated",    # activated=只看激活过的会话 / all=所有托管会话
    "watch_days": 7,               # 激活后多少天内的回复还在观察窗口
    "cooldown_hours": 24,          # 同一会话多久内不重复通知
    "detector": "ai",              # ai=模型二分类 / keywords=纯规则（零成本）
    "pause_auto_reply": True,      # 真人接手后正式关掉该会话 AI 托管
    "skip_ai_reply": True,         # 触发接管的这一条不生成 AI 回复，留给真人
    "handoff_timeout_hours": 2,    # 待接管多久没人接手就恢复 AI 并留记录
}

# 接管记录状态
S_WAITING = "waiting"   # 已通知，等真人接手，托管仍开着
S_TAKEN = "taken"       # 真人已回复，已正式关托管
S_EXPIRED = "expired"   # 超时没人接手，已恢复常规 AI

# 关键词兜底：AI 调不通、或用户关掉 AI 判定时用。宁可漏判，也别把「不需要」
# 误报成意向，让真人白跑一趟。
NEGATIVE = (
    "不需要", "没兴趣", "不用了", "别发了", "不考虑", "暂时不", "以后再说",
    "不做了", "退订", "别再发", "太贵了不", "算了", "拒绝",
)
POSITIVE = (
    "想了解", "了解一下", "有兴趣", "感兴趣", "怎么买", "多少钱", "报价",
    "报个价", "价格", "试用", "体验一下", "演示", "demo", "开通", "账号",
    "合同", "采购", "合作", "对接", "发份", "发一份", "资料", "案例",
    "方案", "续费", "扩购", "立项", "详聊", "细聊", "约个", "聊聊",
    "电话沟通", "能不能做", "什么时候能", "怎么收费", "有没有介绍",
)

CLASSIFY_SYSTEM = (
    "你是一个严格二分类判定器，只判断「客户这条微信消息是否表达了明确的商业意向，"
    "需要真人销售接手」。\n"
    "判 YES：询问价格/报价/合同/采购流程；索要资料/案例/方案；要求试用、开通、演示；"
    "提出具体项目或需求并希望我们配合；主动约会议/电话/见面；明确说想合作、想买、想续费。\n"
    "判 NO：闲聊、寒暄、道谢、单纯确认收到、明确拒绝或没兴趣、投诉售后、纯技术闲问、"
    "只发图片/表情/链接没有文字、以及任何含糊不确定的表述。\n"
    "拿不准一律判 NO。\n"
    "只输出一行：YES 或 NO，后面可用竖线加 15 字以内原因。例：YES|询问报价\n"
    "不要输出任何其他内容。"
)


# ==================== 配置 ====================

def load(cfg=None):
    """有效配置 = config.json 默认 + 看板保存值覆盖（看板改完立即生效）。"""
    if cfg is None:
        from wechat_ui import load_config
        cfg = load_config()
    out = dict(DEFAULTS)
    out.update(cfg.get("handoff") or {})
    try:
        saved = json.loads(monitor_store.get_setting(SETTING_KEY) or "{}")
        if isinstance(saved, dict):
            out.update(saved)
    except Exception:               # noqa: BLE001 —— 配置坏了就退回文件配置
        pass
    out["targets"] = resolve_targets(out.get("targets"))
    out["watch_days"] = int(out.get("watch_days", 7))
    out["cooldown_hours"] = float(out.get("cooldown_hours", 24))
    try:
        out["handoff_timeout_hours"] = max(
            0.0, float(out.get("handoff_timeout_hours") or 2))
    except (TypeError, ValueError):
        out["handoff_timeout_hours"] = 2.0
    out["enabled"] = bool(out.get("enabled", True))
    out["pause_auto_reply"] = bool(out.get("pause_auto_reply", True))
    out["skip_ai_reply"] = bool(out.get("skip_ai_reply", True))
    return out


def save(conf):
    """看板保存：只存可编辑字段，其余走 config.json 默认。"""
    allowed = ("enabled", "targets", "watch_scope", "watch_days",
               "cooldown_hours", "detector", "pause_auto_reply", "skip_ai_reply",
               "handoff_timeout_hours")
    data = {k: conf.get(k) for k in allowed if k in conf}
    data["targets"] = resolve_targets(data.get("targets"))
    monitor_store.set_setting(SETTING_KEY, json.dumps(data, ensure_ascii=False))
    return load(cfg={"handoff": data})


def resolve_targets(raw):
    """把配置里的目标统一成 [{conv_id, name, kind}]。

    看板存的是完整对象；config.json 里允许偷懒只写名字（按通讯录反查），
    两种都支持。查不到的名字直接丢掉并留一条警告，避免发给错误的人。
    """
    out, seen = [], set()
    rows = None
    for t in (raw or []):
        item = None
        if isinstance(t, dict):
            cid = (t.get("conv_id") or "").strip()
            if cid:
                item = {"conv_id": cid,
                        "name": (t.get("name") or cid).strip(),
                        "kind": t.get("kind") or ""}
        else:
            name = str(t or "").strip()
            if not name:
                continue
            if rows is None:
                rows = monitor_store.list_contacts()
            hit = next((r for r in rows
                        if (r.get("name") or "").strip() == name), None)
            if hit is None:
                hit = next((r for r in rows
                            if name in (r.get("name") or "")), None)
            if hit:
                item = {"conv_id": hit["conv_id"],
                        "name": (hit.get("name") or name).strip(),
                        "kind": hit.get("kind") or ""}
            else:
                print("[handoff] 通知目标「%s」在通讯录里没找到，已忽略" % name)
        if not item:
            continue
        if item["conv_id"] in seen:
            continue
        seen.add(item["conv_id"])
        if not item["kind"]:
            item["kind"] = ("group" if item["conv_id"].endswith("@chatroom")
                            else "friend")
        out.append(item)
    return out


# ==================== 判定闸门 ====================

def in_scope(conv_id, conf):
    """这个会话值不值得做意向判定。"""
    if conf.get("watch_scope") == "all":
        return True
    since = int(time.time()) - conf["watch_days"] * 86400
    try:
        return monitor_store.has_recent_outreach(conv_id, since)
    except Exception:               # noqa: BLE001
        return False


def cooling(conv_id, conf):
    """同一会话冷却期内不再通知（客户连说三句只打扰真人一次）。"""
    try:
        last = monitor_store.last_handoff_ts(conv_id)
    except Exception:               # noqa: BLE001
        return False
    return bool(last and time.time() - last < conf["cooldown_hours"] * 3600)


def detect(cfg, conf, text):
    """返回 (是否有意向, 判定依据)。AI 失败自动降级到关键词，绝不抛给主线程。"""
    text = (text or "").strip()
    if not text:
        return False, ""
    mode = (conf.get("detector") or "ai").lower()
    if mode == "ai":
        try:
            ans = ai_client.classify(
                cfg, CLASSIFY_SYSTEM,
                "客户消息：\n%s" % text[:600], timeout=20)
            head = ans.strip().splitlines()[0].strip()
            hit = head.upper().startswith("YES")
            reason = head.split("|", 1)[1].strip() if "|" in head else "AI 判定有意向"
            return hit, (reason or "AI 判定有意向") if hit else ""
        except Exception as e:      # noqa: BLE001
            print("[handoff] AI 判定不可用，降级关键词：%s" % e)
    return _by_keywords(text)


def _by_keywords(text):
    for w in NEGATIVE:
        if w in text:
            return False, ""
    hits = [w for w in POSITIVE if w in text]
    return (True, "命中：" + "、".join(hits[:3])) if hits else (False, "")


# ==================== 通知 ====================

def notice(customer, quote, reason, paused, timeout_hours=0):
    """内部通知正文。paused 现在表示「真人接手后是否会正式交出会话」。"""
    now = datetime.datetime.now().strftime("%m-%d %H:%M")
    q = (quote or "").strip().replace("\n", " ")[:100]
    tail = ("机器人会先接着陪聊，真人在这个会话回复后，AI 托管会自动关闭并交出。"
            if paused else "AI 托管保持开启，请留意别和机器人抢话。")
    if timeout_hours and timeout_hours > 0:
        tail += " 若 %g 小时内无人接手，将恢复常规自动回复。" % timeout_hours
    return ("【客户有意向 · 请人工接管】\n"
            "客户：%s\n"
            "原话：%s\n"
            "判断：%s\n"
            "时间：%s\n"
            "%s\n"
            "在微信里搜「%s」即可找到对话。"
            % (customer, q or "（无文字内容）", reason or "明确意向", now, tail, customer))


def log(conv_id, customer, quote, reason, targets, paused,
        state=S_WAITING, deadline=0):
    """落一条 state='waiting' 的接管记录，返回 id（失败返回 0）。"""
    try:
        return monitor_store.add_handoff_log(
            conv_id, customer, quote, reason, targets, paused,
            state=state, deadline=deadline)
    except Exception as e:          # noqa: BLE001
        print("[handoff] 接管记录写库失败（不影响通知）: %s" % e)
        return 0


def pending():
    """还没收口的接管记录（已通知、托管仍开着）。"""
    try:
        return monitor_store.list_pending_handoffs()
    except Exception as e:          # noqa: BLE001
        print("[handoff] 读待接管列表失败: %s" % e)
        return []


def human_replied(conv_id, since_ts):
    """真人是否在这个客户会话里回过话（机器人发的带 sent_by 标记，不算）。"""
    try:
        return monitor_store.human_replied_since(conv_id, since_ts)
    except Exception as e:          # noqa: BLE001
        print("[handoff] 人工回复检测失败（按未回复处理）: %s" % e)
        return False


def finish(log_id, state):
    """把 waiting 收口成 taken / expired。带状态条件，重复调用不会覆盖终态。"""
    try:
        return monitor_store.resolve_handoff(log_id, state)
    except Exception as e:          # noqa: BLE001
        print("[handoff] 接管状态收口失败: %s" % e)
        return 0
