#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""主动触达 / 存量激活的共享内核。

为什么要单独一个模块
--------------------
「主动找人说话」这件事有两处在用：
  · `auto_reply.py` 的常驻触达线程（每 5 分钟巡检一次，按 config 自己发）
  · `activate.py` 的手动一次性激活（人工看着发）

话术意图、上下文拼装、「谁该被碰」的挑选规则，两边各写一份必然漂移——
`FRIEND_PASS_PATTERNS` 之前就在两个文件里各存过一份，改一处漏一处。
所以统一收敛到这里：改这里，两边同时生效。

两类人选、两套话术
------------------
  · revive    —— 聊过、现在沉默：**承接上次聊到的具体内容**（禁止泛泛问候）
  · icebreak  —— 加了好友从没聊过：自我介绍 + 价值点 + 低压钩子

红线（两套都适用）：不是设计外包、不报价、不被问就不主动说自己是 AI。
"""
import monitor_store

# 「好友通过」的系统提示措辞（2026-10-19 查库实测存在的三种）。
# **注意**：其中「我通过了你的朋友验证请求…」在库里 raw type 是「文本」，
# 与客户真消息同类型——所以只能靠文案识别，不能只靠 type。
FRIEND_PASS_PATTERNS = (
    "我通过了你的朋友验证请求",
    "你已添加了",
    "以上是打招呼的消息",
)


# ==================== 话术意图 ====================

REVIVE_INTENT = (
    "你正在**主动**给一位沉默了一段时间的老客户发微信。\n"
    "注意：他不是刚发消息给你，这是**你**主动找他，所以不要用「收到」「好的」"
    "「您刚才说的」这类回复口吻。\n"
    "目标：把话重新聊起来，不是推销。必须承接下面「上次聊到」里的**具体内容**"
    "（他提过的项目/问题/需求，或我们答应帮他确认的事），让对方觉得你记得他。\n"
    "禁止：泛泛问候（「最近怎么样」「在忙吗」「好久不见」这类一律不许用）、"
    "产品推销、发链接、报价、群发感。\n"
    "要求：1~3 句口语化中文，只问一个问题，让对方容易接话。"
    "只输出消息正文，不要解释。"
)

ICE_INTENT = (
    "你正在微信上给一位**刚加上好友、还没聊过**的客户写第一条消息"
    "（自我介绍 + 破冰）。\n"
    "身份：你是「{persona}」，{company}的在线顾问；公司做的是{positioning}，"
    "核心产品是「{product}」——帮照明/建筑/室内/景观设计师用 AI 出效果图和"
    "方案汇报物料。\n"
    "红线：① 我们卖的是 SaaS 平台/工具，**不是接设计项目的外包服务商**，"
    "绝不说“可以帮您做设计/接项目/代出方案”；② 不报价、不承诺效果、不催单；"
    "③ 不主动说自己是 AI。\n"
    "要求：①一句话自报身份（不念公司简介）；②说明来意，可结合来源/备注，"
    "没有就泛到行业，**绝不编造**对方信息；③给一个具体价值点 + 低压钩子"
    "（“需要的话我发份介绍给您看看”）；④口语化、80 字以内，只输出正文。"
)


def identity(cfg=None):
    """身份四件套（config.json 的 ai 块）。日常回复与这里共用同一份。"""
    if cfg is None:
        from wechat_ui import load_config
        cfg = load_config()
    ai = (cfg.get("ai") or {})
    return {
        "persona": ai.get("persona") or "小光",
        "company": ai.get("company") or "中照金智（广州）数据科技",
        "product": ai.get("product") or "AI照明工作站（A站）",
        "positioning": ai.get("positioning") or "为设计提效的 AI SaaS 平台",
    }


# ==================== 上下文 ====================

def build_context(conv_id, lookback_days=180, max_lines=6, quiet=True):
    """拼「上次聊到哪」：最近几条真实消息 + 距今多少天。拿不到返回空串。

    为什么不用朋友圈当由头（用户 2026-10-21 明确）：朋友圈大量是转发、行业杂讯，
    拿它搭话很容易「看到您发的那个…」说错对象。聊天记录是我们自己发的、对方
    自己说的，语义和归属都确定。
    """
    since = int(__import__("time").time()) - int(lookback_days) * 86400
    try:
        msgs = monitor_store.get_messages(conv_id, limit=12, since_ts=since)
    except Exception as e:              # noqa: BLE001
        if not quiet:
            print("  [warn] 取历史失败：%s" % e)
        return ""
    lines, last_ts = [], 0
    for m in msgs:
        t = (m.get("text") or "").strip()
        if not t:
            continue
        if any(p in t for p in FRIEND_PASS_PATTERNS):
            continue                    # 好友通过/打招呼这类系统提示不算聊天
        lines.append("%s：%s" % ("我" if m.get("role") == "me" else "对方", t[:120]))
        last_ts = m.get("ts") or last_ts
    if not lines:
        return ""
    import time
    days = max(0, int((time.time() - last_ts) / 86400)) if last_ts else 0
    return "（最后一条距今约 %d 天）\n%s" % (days, "\n".join(lines[-max_lines:]))


# ==================== 意图拼装 ====================

def revive_intent(ctx, override=""):
    """唤回话术：把「主动 vs 回复」和「不许空泛问候」写死。

    不写死的话 AI 会当成一次普通回复，生成「最近怎么样」「在忙吗」这种废话——
    既不像真人，也是最容易被当成营销号的行为。
    """
    base = (override or "").strip() or REVIVE_INTENT
    if ctx:
        return base + "\n\n【上次聊到（真实聊天记录，供你承接，不要照抄）】\n" + ctx
    return base


def icebreak_intent(cfg=None):
    return ICE_INTENT.format(**identity(cfg))


# ==================== 候选挑选 ====================

def pick_targets(ou, limit=10, now=None):
    """按 config 的 outreach 块挑候选。

    ou 就是 config.json 里的 `outreach` 字典（调用方通常已经把看板可改的
    开关合进来），键的含义见 config.json 注释版说明（RUNBOOK 六）。这里只做
    参数换算，规则本身在 monitor_store.get_activation_targets（一处实现，
    两处调用）。「不打扰」名单（outreach_skip）在那边统一排除，这里不用管。
    """
    ou = ou or {}
    return monitor_store.get_activation_targets(
        # silent_days 是「多少天没动算沉默」；底层按小时算，这里换算过去
        silent_hours=float(ou.get("silent_days", 7)) * 24,
        max_days=int(ou.get("max_days", 0) or 0),
        min_gap_days=int(ou.get("min_gap_days", 7)),
        max_per_contact=int(ou.get("max_per_contact", 0) or 0),
        include_no_chat=bool(ou.get("include_new_contacts", False)),
        require_switch=bool(ou.get("require_switch", False)),
        limit=limit, now=now)
