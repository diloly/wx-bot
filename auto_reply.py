#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
auto_reply.py —— 自动回复闭环：实时收消息 -> 大模型生成 -> 键盘发送 -> 写回看板
=================================================================================
链路（三级流水线，2026-10-16 起）：
    1. 收：wx_recv_bridge.poll_new() 轮询 WeChatDB（本地 SQLCipher 解密，只读）
    2. 攒：收到的消息先攒进该会话的「合并缓冲」，**不立刻建任务**；用户静默
       `auto_reply.merge_window_seconds`（默认 8s）不再发新消息，才把这几条
       **合并成一条「回复任务」**投递（类型/群/白名单/会话开关在入缓冲时判，
       每小时上限在真正投递那一刻判）
    3. 想（AI 线程）：串行消费待生成队列，一条 AI 请求完成就立刻发起下一条，
       **不等上一条发出去**
    4. 发（发送线程）：独立消费待发送队列，在 UI 门内按键盘流程发送
    5. 记：发送成功后由 send_to_contact 写回 monitor_store(role=me)，看板即时可见

为什么生成与发送要解耦（2026-10-16 用户拍板）：
    原来是一条龙串行——「生成 A → 发送 A → 生成 B」——发送（要抢微信 UI，几秒）
    白白占着 AI 的等待窗口。现在 AI 线程只管生成、生成完立刻接下一单；发送线程
    只管在 UI 门里发。两条流水线各跑各的，互不阻塞。
    **顺序仍有保证**：AI 线程是单线程、按 FIFO 消费，产出的发送队列顺序＝入队
    顺序；两边各只有一个 worker，不会乱序。

任务与超时（2026-10-16 用户拍板）：
    一条「回复任务」= **一个用户的一次回复**（不是一条消息、也不是一次 AI 请求），
    从投递到消息发出算一条，带自己的时间预算 `auto_reply.task_timeout_seconds`
    （默认 120s）。预算覆盖 AI 生成 + 排队 + 发送三段，任何一段耗尽就作废并记
    日志——这就是「对单条任务做限制」的落点。

    合并窗口与「已处理到哪条」（2026-10-16 用户拍板，替代旧的 cooldown_seconds）：
    客户常分两三条才说完一个意思，逐条发 AI 只会答半句话。所以收到消息先攒进
    该会话缓冲，静默 `merge_window_seconds` 秒才合并成一条任务 —— 缓冲本身就是
    「已处理到哪条」的记录：flush 清空缓冲 = 把之前收到的全部消息标记为「已交给
    AI」。因此「回复发完之后这个记录结点后面还有消息」时，那些消息已在新缓冲里，
    再等一个窗口就会回下一轮。**旧的 cooldown_seconds 已废弃**：它按「距上次建
    任务不足 N 秒就跳过」判，会把「回复发出去之后又补的一句」直接丢掉；而合并
    窗口本就保证「用户没停手就不回复」，不需要再叠冷却。

⚠️ 前提与风险：
- **总开关 `config.auto_reply.enabled`**：false 时本进程只收消息供看板监控、不回复任何人。
- 键盘发送会抢占键鼠焦点，运行时不要人工操作电脑（鼠标甩到左上角可 FAILSAFE 急停）。
- dry_run 默认 true：只打印「将回复什么」，不真发。确认无误后 --no-dry 真发。
- 频率受 max_replies_per_hour + 合并窗口 + rhythm 拟人节流共同限制，防行为风控。
- 只维护已知客户；群消息默认不自动回（allow_groups=false，群 wxid 以 @chatroom 结尾）。
- 消息 type 是中文字符串（"文本"/"图片"/...），reply_types 按字符串配。
- 本脚本已内含收消息轮询，不要再同时跑 wx_recv_bridge.py（二选一）。
- 抢微信 UI 的动作（发送/朋友圈）统一走 ui_gate 的跨进程排队门，见 ui_gate.py。

用法：
    python auto_reply.py                # dry-run 观察（推荐先跑这个）
    python auto_reply.py --no-dry       # 真发（会抢焦点，慎用）
    python auto_reply.py --interval 5   # 自定义轮询间隔秒数
"""
import argparse
import json
import queue
import random
import re
import sys
import threading
import time

import handoff
import monitor_store
import outreach_core as oc
import ui_gate
import db_gate
from ai_client import generate as ai_generate
from wechat_ui import (WeChatPCBot, load_config, SendNotVerified,
                       SendUnconfirmed, SendAborted)
from wx_recv_bridge import poll_new, is_self, me_username

GROUP_SUFFIX = "@chatroom"

# 「好友通过」的系统提示措辞与「主动触达」话术都收敛在 outreach_core 里，
# 因为 activate.py 也要用同一份，两处各存一份改一处必漏一处（踩过）。
FRIEND_PASS_PATTERNS = oc.FRIEND_PASS_PATTERNS
OUTREACH_INTENT = oc.REVIVE_INTENT


class ReplyTask:
    """一条「回复任务」：从投递到消息发出算一条，粒度＝一个用户的一次回复。

    deadline 在**投递时**就定死（= 投递时刻 + task_timeout），所以「排队等候」
    也算在预算里——这正是用户要的「对单条任务做限制」：一条任务不能因为前面
    排了别人就无限期挂着。
    """
    __slots__ = ("id", "conv_id", "name", "text", "created", "deadline", "reply",
                 "kind", "at_name", "variant", "by", "campaign_id")

    def __init__(self, conv_id, name, text, timeout, task_id=None, kind="reply",
                 at_name=None, variant="revive", by="ai", campaign_id=None):
        self.id = task_id          # reply_task 表主键（落库后回填；None=未落库）
        self.conv_id = conv_id
        self.name = name
        self.text = text            # 触发本任务的客户原话（AI 的 extra_context）
        self.created = time.time()
        self.deadline = self.created + float(timeout)
        self.reply = None           # AI 生成后回填
        self.kind = kind            # 'reply'=正常回复；'first_touch'=通过后的首条激活
        # 群聊专用：发送前要 @ 的成员显示名（群昵称）。None=私聊/不 @
        self.at_name = at_name
        # 主动触达子类型：revive=承接旧话题 / icebreak=从没聊过的破冰
        self.variant = variant
        # 消息库里的「谁发的」标签：ai / manual / handoff / campaign
        self.by = by
        # 活动触达：发完要把结果回写到 campaign.result（None=普通任务）
        self.campaign_id = campaign_id

    def remaining(self):
        return self.deadline - time.time()

    def expired(self):
        return self.remaining() <= 0


def _thread_com_init(name):
    """给**当前线程**初始化 COM，返回一个持有者对象（失败返回 None）。

    为什么必须做（2026-10-16）：UIAutomation 是 COM 组件，Windows 要求
    **每个使用它的线程各自 CoInitialize**。没初始化时，首次创建 UIA 控件会抛
    CO_E_NOTINITIALIZED(-2147221008)，uiautomation 库重试 3 次后打印
    「尚未调用 CoInitialize / Can not load UIAutomationCore.dll」并抛异常——
    **那一次定位/切页就是失败的**，只是被上层的 try/except 吞掉，表现成偶发失败。
    （整个 wechatauto 库只有 `uia_driver.WeChatUIA.__init__` 一处 CoInitialize，
    `moment.py` 的 ControlFromHandle 等路径没有。）

    为什么现在才冒出来：改造前发送/朋友圈都在**主线程**跑，主线程被 UIA 那条路
    顺手初始化过；把这两类操作搬进独立 worker 线程后，新线程是「裸」的。

    返回对象**必须活到线程结束**（它 __del__ 时 CoUninitialize），故调用方要
    把他存成局部变量/在 finally 里显式 Uninitialize。
    """
    try:
        from uiautomation import UIAutomationInitializerInThread
        return UIAutomationInitializerInThread()
    except Exception as e:      # noqa: BLE001
        print("[auto] %s：线程 COM 初始化失败（UIAutomation 类操作可能不可用）: %s"
              % (name, e))
        return None


class AutoReplier:
    def __init__(self, cfg):
        self.cfg = cfg
        ar = cfg.get("auto_reply", {})
        self.interval = ar.get("interval_seconds", 10)
        self.intent = ar.get(
            "intent",
            "你正在以客户维护助手的身份回复客户的微信消息。语气自然、简洁、口语化；"
            "不确定的问题坦诚说需要确认，不编造。只输出回复正文。",
        )
        self.allowed = set(ar.get("contacts", []) or [])
        self.allow_groups = ar.get("allow_groups", False)
        # 合并窗口（debounce）：一条消息收到后先不建任务，攒进该会话的待合并缓冲；
        # 用户停手 merge_window 秒（缓冲不再刷新）才把这几条**合并成一条任务**交给
        # AI。用户 2026-10-16 拍板：**废弃 cooldown_seconds**（它按「距上次建任务
        # 不足 N 秒就跳过」判，会把「回复发出去之后又补的一句」直接丢掉，而合并窗口
        # 本身已经保证「用户没停手就不回复」，无需再叠一层冷却）。
        self.merge_window = float(ar.get("merge_window_seconds", 8))
        # 发送结果校验的等待上限（秒）：按完发送键后回读微信库，等它把消息写进去。
        # 微信写库有 1~3s 延迟，给到 6s 足够；读不到就判「未真正发出」，不写库。
        self.confirm_timeout = float(ar.get("confirm_timeout_seconds", 6))
        self.max_per_hour = ar.get("max_replies_per_hour", 20)
        self.context_turns = ar.get("context_turns", 3)    # 带给AI的最近对话轮数（1轮=1来1回）
        self.context_hours = ar.get("context_hours", 6)    # 上下文时间窗：更早的历史不带
        self.reply_types = set(ar.get("reply_types", ["文本"]))
        self.enabled = ar.get("enabled", False)   # 总开关：false 时只监控上屏，不回复任何人
        self.dry_run = ar.get("dry_run", True)
        # 单条回复任务的时间预算（秒）：覆盖 AI 生成 + 排队 + 发送三段。
        # 用户 2026-10-16 拍板「对超时，对单条任务做限制，每个消息的回复
        # （每个用户算一条）算一条任务」——超时不再用全局排队上限，而是每条任务
        # 各带一份预算，耗尽即作废（记日志），不会无限期挂在队列里。
        self.task_timeout = float(ar.get("task_timeout_seconds", 120))
        self.bot = WeChatPCBot({
            "dry_run": self.dry_run,
            "send_delay_seconds": cfg.get("send_delay_seconds", 5),
        })
        # conv_id -> 待合并缓冲 {name, texts[], first_ts, last_ts}：收到的消息先攒这，
        # 静默 merge_window 秒后才合并成一条任务（这就是「已处理到哪条」的账）。
        # 只被主线程读写（poll_new 之后、flush 之内），无需加锁。
        self._pending = {}
        self._hour_window = []  # 最近一小时的建任务时间戳
        self.switches = {}      # conv_id -> bool：每会话AI自动回复开关（默认全关，monitor_store.conv_switch 表）
        self._contact_cache = {}  # conv_id -> contact dict：反查结果缓存，避免重复查库
        self.rhythm_profile = ar.get("rhythm", "natural")  # 拟人节流档位（natural/calm/fast/off）
        # ---------- 加好友：名单节奏 / 通过检测 / 首条激活（2026-10-19） ----------
        af = cfg.get("add_friend", {}) or {}
        self.af_enabled = bool(af.get("enabled", False))
        self.af_daily_limit = int(af.get("daily_limit", 10))
        self.af_gap_min = float(af.get("interval_min_seconds", 480))    # 8 分钟
        self.af_gap_max = float(af.get("interval_max_seconds", 900))    # 15 分钟
        self.af_work_start = int(af.get("work_start_hour", 9))
        self.af_work_end = int(af.get("work_end_hour", 21))
        self.af_weekend_factor = float(af.get("weekend_factor", 0.5))
        self.af_max_fail = int(af.get("max_consecutive_failures", 3))
        self.af_retry_days = int(af.get("retry_days", 30))
        self.ft_delay_min = float(af.get("first_touch_delay_min", 60))
        self.ft_delay_max = float(af.get("first_touch_delay_max", 120))
        self.ft_intent = (af.get("first_touch_intent") or "").strip()
        self._af_fail_streak = 0      # 连续技术失败次数（只有 add worker 读写）
        self._af_paused_day = ""      # 因连续失败而停手的那一天
        self._af_last_at = 0.0        # 上次尝试的时刻（用于间隔闸）
        self._af_limit_logged = False # 「今日已达上限」只打印一次
        # conv_id -> {due, name, note, source, tid}：通过后待发的首条
        self._first_touch = {}
        self._pass_seen = set()       # 通过事件去重（conv_id|ts）
        # ---------- 主动触达：唤醒长期沉默的客户（2026-10-21） ----------
        # 用户口径：朋友圈不能当由头（大量是转发/杂讯，容易说错对象）。所以
        # 由头只从 messages 里取——「对方最后一句我们没接」或「上次聊到的内容」，
        # 拿不到具体由头就不发（宁可少发，也不发空泛问候）。
        ou = cfg.get("outreach", {}) or {}
        self.ou_cfg = ou                # 原样留着：候选规则统一交给 outreach_core
        self.ou_enabled = bool(ou.get("enabled", False))
        # 0 = 不设每日上限（靠条间间隔 + 每会话冷却节流）；想恢复上限填正整数
        self.ou_daily_limit = int(ou.get("daily_limit", 0) or 0)
        self.ou_gap_min = float(ou.get("interval_min_seconds", 600))   # 10 分钟
        self.ou_gap_max = float(ou.get("interval_max_seconds", 1800))  # 30 分钟
        self.ou_work_start = int(ou.get("work_start_hour", 8))
        self.ou_work_end = int(ou.get("work_end_hour", 21))
        self.ou_silent_days = int(ou.get("silent_days", 7))       # 多久没动算沉默
        self.ou_max_days = int(ou.get("max_days", 0) or 0)        # 0=不限（越久越该激活）
        self.ou_min_gap_days = int(ou.get("min_gap_days", 7))     # 同一人冷却（7 天 1 次）
        # 0=不启用。设 7 则「整体 7 天才跑一轮」：上次真实触达后 7 天内不再碰任何人。
        # 这是比 per-contact 冷却更狠的全局降频闸，按需开。
        self.ou_cycle_days = int(ou.get("cycle_days", 0) or 0)
        self.ou_max_per_contact = int(ou.get("max_per_contact", 3))
        self.ou_lookback_days = int(ou.get("lookback_days", 180)) # 取历史对话的窗口
        # 加了好友但从没聊过的人，是否也做破冰（默认**不**：给没说过话的人主动
        # 发消息观感最像群发营销，要开必须显式写 true）
        self.ou_include_new = bool(ou.get("include_new_contacts", False))
        self.ou_intent = (ou.get("intent") or "").strip()
        self._ou_day = ""             # 已提示「今日达上限」的那一天（只打印一次）
        self._ou_next_at = 0.0        # 下一次允许触达的时刻（间隔在投递时抽一次定死）
        # 活动触达：cid -> {content, targets, ids, queued}（内存态，库里有终态账本）
        self._campaign_active = {}
        self._campaign_lock = threading.Lock()
        # 群聊：我在每个群的群昵称（@ 匹配用）+ 缓存时间。群昵称是会变的
        # （用户随时能改），所以缓存 10 分钟过期，不能永久缓存。
        self._group_nick = {}         # chatroom -> {昵称, ...}
        self._group_nick_at = {}      # chatroom -> 缓存时刻
        self.db = None                    # 主线程用（收消息轮询）
        self._ai_db_obj = None            # AI 线程用（联系人反查），避免跨线程共享连接
        self._send_db_obj = None          # 发送线程用（发送结果校验回读），同上
        self._moment_ops = None          # 朋友圈模块（惰性创建，见 _poll_moments）
        self._last_moment_refresh = 0.0  # 上次 UI 刷新朋友圈的时间戳
        # ---------- 流水线：待生成 -> 待发送 ----------
        # AI 线程与发送线程各一个（单 worker 保证 FIFO 不乱序）。队列有上限，
        # 满了就拒收新任务并记日志——不然一个卡住的 AI 会把内存和队列拖爆。
        self.queue_max = int(ar.get("queue_max", 200))
        self._ai_q = queue.Queue(maxsize=self.queue_max)     # 待 AI 生成
        self._send_q = queue.Queue(maxsize=self.queue_max)   # 待发送（AI 已生成好）
        self._stop = threading.Event()
        self._threads = []

    # ---------- 判定闸门 ----------
    def _is_allowed(self, conv_id, name):
        """群/白名单闸（会话开关闸在 collect()/flush() 里单独判，静默跳过）。"""
        if conv_id.endswith(GROUP_SUFFIX) and not self.allow_groups:
            return False
        if self.allowed and not (conv_id in self.allowed or name in self.allowed):
            return False
        return True

    def _rate_ok(self, now):
        self._hour_window = [t for t in self._hour_window if now - t < 3600]
        return len(self._hour_window) < self.max_per_hour

    # ---------- 群聊：只有 @ 我才回 ----------
    # 群里的消息默认**不接**：几十条聊天里可能只有一句在问我们，全回就是刷屏，
    # 而且会把别人之间的对话当成问自己的。判据只有一条——文本里出现
    # 「@<我在这个群的昵称>」。昵称不是猜的：get_group_members 能拿到自己在该群
    # 的显示名（实测 8 个群都取得到）。群昵称随时可改，所以缓存 10 分钟。
    def _my_group_nicks(self, chatroom):
        now = time.time()
        cached = self._group_nick.get(chatroom)
        if cached is not None and now - self._group_nick_at.get(chatroom, 0) < 600:
            return cached
        nicks = set()
        try:
            me = (self.db.get_self_info() or {}) if self.db else {}
            me_wxid = str(me.get("username") or "")
            for m in (self.db.get_group_members(chatroom) or []):
                if str(m.get("username")) != me_wxid:
                    continue
                for k in ("nick_name", "remark"):
                    v = (m.get(k) or "").strip()
                    if v:
                        nicks.add(v)
        except Exception as e:      # noqa: BLE001
            print("[auto] 读群昵称失败(%s): %s" % (chatroom, e))
        if not nicks:               # 群里没设昵称时，微信 @ 用的是全局昵称
            try:
                g = (self.db.get_self_info() or {}) if self.db else {}
                if g.get("nick_name"):
                    nicks.add(str(g["nick_name"]).strip())
            except Exception:       # noqa: BLE001
                pass
        self._group_nick[chatroom] = nicks
        self._group_nick_at[chatroom] = now
        return nicks

    def _at_me(self, text, chatroom):
        """这条群消息是否 @ 了我。微信 @ 后面跟的是 U+2005（四分之一空格），
        但不同版本也可能用普通空格，两种都认。「@所有人」不会匹配到昵称。"""
        if not text or "@" not in text:
            return False
        for n in self._my_group_nicks(chatroom):
            if ("@" + n + "\u2005") in text or ("@" + n + " ") in text:
                return True
        return False

    @staticmethod
    def _group_sender(text):
        """群消息前缀 → 发送者显示名：'群备注: wxid_xxx:\\n正文' → '群备注'。

        前缀是 wx_recv_bridge 入库时拼的（群聊消息都带），不用再查库。
        解析不出就返回空串，调用方按「不 @」处理。
        """
        m = re.match(r"^([^:\n]{1,40}):\s*wxid_[^:\n]{1,40}:\s*\n", text or "")
        return m.group(1).strip() if m else ""

    def _contact_of(self, conv_id, name):
        for c in self.cfg.get("contacts", []):
            if c.get("name") == name or c.get("wxid") == conv_id:
                return c
        # config 没配这个联系人：用库反查补全（wxid 精确匹配 + 微信备注进 note）。
        # 备注比昵称更可能是客户对他的称呼（「群备注」），喂给 AI 后称呼不会出错。
        cached = self._contact_cache.get(conv_id)
        if cached is not None:
            return cached
        info = {"name": name, "tags": [], "note": ""}
        try:
            # 统一走 db_gate 的进程单例。以前这里「为线程安全」另建一个
            # WeChatDB，反而更危险：库的构建锁是实例级的，多实例会并发
            # replace 同一份解密副本（WinError 5）。库每次查询都是新开连接
            # 用完即 close，共享对象本身不共享 sqlite 连接，是安全的。
            if self._ai_db_obj is None:
                self._ai_db_obj = db_gate.get_db()
            rows = self._ai_db_obj.search_contact(name) or []
            # 精确匹配本会话 wxid 优先；搜不到（名字是昵称/别称）就取首行，
            # 备注仍有参考价值——丢什么都不丢备注。
            match = next((r for r in rows if r.get("username") == conv_id), None)
            r = match or (rows[0] if rows else None)
            if r:
                if match:
                    info["wxid"] = conv_id
                remark = r.get("remark") or ""
                if remark:
                    info["note"] = "微信备注：" + remark
        except Exception:
            pass
        self._contact_cache[conv_id] = info
        return info

    def _build_intent(self, conv_id):
        """把最近几轮对话拼进 intent，让回复有语境（不隔空答话）。
        双重截断：① 只取最近 context_turns 轮；② 只取 context_hours 小时内的消息。
        更早的历史不带——防止上下文过长，也防止拿过期话题接话。"""
        intent = self.intent
        since = int(time.time()) - self.context_hours * 3600
        try:
            ctx = monitor_store.get_messages(
                conv_id, limit=self.context_turns * 2, since_ts=since)
        except Exception:
            ctx = []
        if ctx:
            lines = [
                "%s: %s" % ("我" if m["role"] == "me" else "对方", m["text"])
                for m in ctx
            ]
            intent = intent + "\n【最近对话（%d小时内，供参考语境）】\n" % self.context_hours \
                     + "\n".join(lines)
        if conv_id.endswith(GROUP_SUFFIX):
            # 群聊的上下文天然是多人混在一起的：不点明的话，AI 很容易把别人之间
            # 的对话当成在问自己，答非所问。这里把场景和边界写死。
            intent += ("\n【场景】这是微信群聊，群里多人在说话，消息带「发送者: 内容」"
                       "前缀。只有明确 @ 你的那条是在问你；不要回应其他人之间的话题，"
                       "也不要在回复里罗列群成员的发言。你的身份是 **AI 助手**，在群里"
                       "不要冒充真人；必要时可以用半句话自报（例如「我是 AI 助手小光」）。")
        return intent

    # ---------- 任务状态账本（内存队列的持久化镜像，仅供看板「看得见」） ----------
    # 内存 queue 才是真正的工作队列（worker 靠它阻塞取值、保证 FIFO）；reply_task
    # 表是状态账本。两个进程不共享内存，看板（另一个进程）只能通过库看见队列。
    # 约定：账本写失败**绝不影响业务**——它只负责可观测，不能因为它坏了就不回消息。
    def _task_open(self, task):
        """任务落库（pending），返回 id；库异常时返回 None。"""
        try:
            return monitor_store.add_task(
                task.conv_id, task.name, task.text, task.deadline,
                kind=task.kind, created=task.created)
        except Exception as e:      # noqa: BLE001
            print("[auto] 任务落库失败（不影响本次回复）: %s" % e)
            return None

    def _task_set(self, task, status, reply=None, reason=None):
        """更新任务状态；task.id 为空（未落库）时静默跳过。"""
        if not task.id:
            return
        try:
            monitor_store.update_task(task.id, status=status,
                                      reply=reply, reason=reason)
        except Exception as e:      # noqa: BLE001
            print("[auto] 任务状态写库失败(id=%s, status=%s): %s"
                  % (task.id, status, e))

    # ---------- 合并窗口（debounce）：先攒批，用户停手才建任务 ----------
    # 用户 2026-10-16 定的规则：**收到消息不立刻发 AI**——攒进该会话缓冲，静默
    # merge_window 秒（期间不再有新消息）才把这几条合并成一条任务交给 AI。
    # 目的：客户常分两三条才说完一个意思，逐条发 AI 会答半句话；合并后 AI 看到
    # 完整意思，且这几条也一起进了任务账本（看板可见）。
    # 同时这也是「已处理到哪条」的记录：flush 一清空缓冲，就等于把之前收到的
    # 全部消息标记为「已交给 AI」；回复发完后若又有新消息进缓冲，就是「这条记录
    # 的消息之后还有消息」→ 再等一个窗口，回下一轮。
    def collect(self, item):
        """把一条新消息攒进该会话的待合并缓冲（**不建任务**）。

        静态闸门（总开关 / 自己发的 / 类型 / 群与名单 / 会话开关）在这一层判——
        不放进缓冲的消息，窗口期自然也不会被合并提交。动态闸门（每小时上限）
        留到 flush 那一刻判。
        """
        if not self.enabled and not self.af_enabled:
            return
        if item.get("role") == "me":
            return  # 自己发的消息不触发回复（防自问自答循环）
        conv_id, name = item["conv_id"], item["name"]
        # 系统消息一律不进 AI 缓冲；「好友通过」在这里被识别成事件处理。
        # 注意这一步**不能**放在 enabled 闸之后：加好友是独立开关，
        # 只开加好友、关掉自动回复时，通过事件照样要能检测到。
        if self._handle_system(item):
            return
        # 客户已经主动说话 → 取消脚本化的首条（否则会先发模板再回话，很机械）
        self._cancel_first_touch(conv_id)
        if not self.enabled:
            return
        if item.get("type") not in self.reply_types:
            print("[auto] 跳过非回复类型(%s): %s" % (item.get("type"), name))
            return
        if not self._is_allowed(conv_id, name):
            print("[auto] 跳过(群/名单外): %s" % name)
            return
        # 群聊：只有 @ 我才接。没 @ 就静默丢弃，不进缓冲——注意是**静默**，
        # 群消息量大，每条都打日志会把终端刷满。
        at_name = None
        if conv_id.endswith(GROUP_SUFFIX):
            raw_text = item.get("text") or ""
            if not self._at_me(raw_text, conv_id):
                return
            at_name = self._group_sender(raw_text)
            print("[auto] 群里 @ 我：%s（提问者 %s）"
                  % (name, at_name or "未知"))
        # 每会话自动回复开关（默认关；没开的会话静默不进缓冲，不刷屏）
        if not self.switches.get(conv_id, False):
            return
        now = time.time()
        buf = self._pending.get(conv_id)
        if buf is None:
            buf = {"name": name, "texts": [], "first_ts": now, "last_ts": now,
                   "at_name": at_name}
            self._pending[conv_id] = buf
        if at_name and not buf.get("at_name"):
            buf["at_name"] = at_name    # 群里多条合并时，记住是谁 @ 的
        buf["name"] = name
        buf["texts"].append(item.get("text", ""))
        buf["last_ts"] = now                    # 每来一条就把窗口往后推
        print("[auto] 收到 %s 第 %d 条（%ds 无新消息后合并提交）: %s"
              % (name, len(buf["texts"]), int(self.merge_window),
                 (item.get("text") or "")[:30].replace("\n", " ")))

    def _flush_due(self):
        """把静默窗口已到期的会话攒批合并提交。主循环 1s 粒度调用。"""
        now = time.time()
        for conv_id in list(self._pending.keys()):
            buf = self._pending[conv_id]
            if now - buf["last_ts"] < self.merge_window:
                continue
            del self._pending[conv_id]
            self.flush(conv_id, buf)

    def flush(self, conv_id, buf):
        """把攒好的一批消息合并成**一条**任务投递到「待生成」队列。

        动态闸门（会话开关 / 每小时上限）在这一刻判——从收到第一条到投递之间
        用户可能在看板上把开关关掉，那就直接丢弃这批（开关就是放行标准）。
        """
        name = buf["name"]
        texts = buf["texts"]
        if not self.switches.get(conv_id, False):
            print("[auto] %s 开关已关，丢弃已攒的 %d 条" % (name, len(texts)))
            return
        now = time.time()
        if not self._rate_ok(now):
            print("[auto] 已达每小时上限(%d条)，本批不提交: %s" % (self.max_per_hour, name))
            return
        self._hour_window.append(now)
        merged = "\n".join(t for t in texts if t)   # 多条按收到顺序拼接，喂给 AI
        task = ReplyTask(conv_id, name, merged, self.task_timeout,
                         at_name=buf.get("at_name"))
        task.id = self._task_open(task)        # 状态账本（看板可观测）
        try:
            self._ai_q.put_nowait(task)
        except queue.Full:
            print("[auto] 待生成队列已满(%d 条)，本批不回复: %s" % (self.queue_max, name))
            return
        print("[auto] 合并 %d 条为一条任务 → %s（预算 %.0fs，待生成队列 %d 条）"
              % (len(texts), name, self.task_timeout, self._ai_q.qsize()))

    # ---------- 好友通过检测 / 首条激活 ----------
    # 为什么要有这一层（2026-10-19）：微信把「我通过了你的朋友验证请求…」这条
    # 系统提示的 raw type 记成**文本**，跟客户真消息一模一样。不单独拦，新好友
    # 一通过，AI 就会去回这句话；而如果为了躲它把开关全关掉，客户主动说话又没人接。
    # 所以拆成：系统消息 → 事件（不对话）；真人消息 → 正常对话。
    def _handle_system(self, item):
        """系统消息一律不进 AI 缓冲，返回 True 表示「已在这个分支消化掉」。"""
        text = item.get("text") or ""
        typ = item.get("type") or ""
        if typ == "系统消息":
            self._maybe_friend_pass(item)
            return True
        if any(p in text for p in FRIEND_PASS_PATTERNS):
            self._maybe_friend_pass(item)
            return True
        return False

    def _maybe_friend_pass(self, item, quiet=False):
        """识别「好友通过」并排期首条激活。

        归属判定：**用「通过事件与申请发出的时间距离」认领**，不看会话历史。

        为什么换掉旧判据（2026-10-19 实测三个测试对象全部误判）：旧实现用
        `conv_existed_before(conv_id, sent_at)`——「申请之前该会话有没有消息」——
        来判「是不是刚通过的新好友」。但
          ① 删了好友再加回来，微信**保留历史消息**，这个判据必然为真；
          ② 我们自己那条申请语写库的时间比 sent_at 早 1 秒，连全新会话都会被
             判成「老会话」。
        两个原因叠起来，要激活的人一个都匹配不上，于是既不标 passed、也不排
        首条，看板永远显示「已申请」。

        现在的规则：申请已发出（容差 120s，覆盖写库先后的倒挂）且未超过
        retry_days 的才是候选，取与通过事件时间差最小的那条。队列是串行发的，
        这个信号在实际数据上极准（实测差距 0~1 秒）。
        """
        conv_id = item.get("conv_id") or ""
        text = item.get("text") or ""
        if not conv_id or not any(p in text for p in FRIEND_PASS_PATTERNS):
            return
        try:
            item_ts = int(item.get("ts") or time.time())
        except (TypeError, ValueError):
            item_ts = int(time.time())
        key = "%s|%s" % (conv_id, item_ts)
        if key in self._pass_seen:
            return
        self._pass_seen.add(key)

        try:
            cands = monitor_store.get_friend_sent(limit=20) or []
        except Exception as e:      # noqa: BLE001
            print("[auto] 读待确认名单失败: %s" % e)
            cands = []
        pool = []
        for c in cands:
            sa = c.get("sent_at")
            if not sa:
                continue
            gap = item_ts - int(sa)
            if gap < -120 or gap > int(self.af_retry_days * 86400):
                continue            # 申请还没发出 / 早已过期，都不是这次通过
            pool.append((abs(gap), c))
        matched = min(pool, key=lambda t: t[0])[1] if pool else None
        # 搜索词必须是**微信备注/昵称**：Ctrl+F 只认昵称和备注，喂 wxid 搜不到
        # （2026-10-19 实测：名单里没填 name 的两条首条激活，全挂在
        # 「wxid_xxxxxxxx」这种纯 ID 上，任务落 failed）。
        # 三级回退：消息里的昵称 → 名单里填的 name → 联系人库反查 → wxid（保底）。
        name = item.get("name") or (matched.get("name") if matched else None) or ""
        if not name:
            name = self._nick_of(conv_id) or conv_id
        if matched:
            try:
                monitor_store.update_friend_task(matched["id"], status="passed",
                                                 conv_id=conv_id,
                                                 passed_at=int(time.time()))
            except Exception as e:  # noqa: BLE001
                print("[auto] 标记通过失败(task=%s): %s" % (matched.get("id"), e))
            print("[auto] 好友通过：%s（名单 #%s %s）"
                  % (name, matched["id"], matched.get("keyword")))
        else:
            if not quiet:
                print("[auto] 好友通过但未匹配到名单项：%s（可能是手动加的）" % name)

        # 自动打开该会话的 AI 开关：不打开，客户主动说话也没人接
        try:
            if not self.switches.get(conv_id, False):
                monitor_store.set_switch(conv_id, True)
                self.switches[conv_id] = True
                print("[auto] 已自动打开该会话的自动回复开关：%s" % name)
        except Exception as e:      # noqa: BLE001
            print("[auto] 自动开会话开关失败（不影响首条）: %s" % e)

        # 只有匹配到名单项才排首条：来源不明的人不主动打扰。
        # 另外客户在通过之后已经说过话的也不排——首条激活是为了打破沉默，人家
        # 自己开口了还去打招呼只会显得多余。实时流程里 collect 会用
        # _cancel_first_touch 兜住，但**启动补跑**时客户的话是既成事实，不会
        # 再有新消息来触发取消，必须在这里就判掉。
        if matched and self._has_real_message_after(conv_id, item_ts):
            print("[auto] 首条激活不排期（客户已主动说话）：%s" % name)
        elif matched:
            # 错开：一批客户同时通过时（启动补跑尤其容易一次排好几条），不要
            # 挤在同一分钟里发出去——那看着就是群发。以「已排的最后一条」为
            # 基准往后顺延，每条间隔 ft_delay_min~max，像一个个单独打招呼。
            base = time.time()
            if self._first_touch:
                base = max(base, max(f["due"] for f in self._first_touch.values()))
            delay = random.uniform(self.ft_delay_min, self.ft_delay_max)
            self._first_touch[conv_id] = {
                "due": base + delay,
                "name": name,
                "note": matched.get("note") or "",
                "source": matched.get("source") or "",
                "tid": matched["id"],
            }
            print("[auto] 首条激活已排期：%s，%.0fs 后（客户先说话则取消）"
                  % (name, self._first_touch[conv_id]["due"] - time.time()))

    def _nick_of(self, conv_id):
        """conv_id → 微信备注/昵称（Ctrl+F 的搜索词只能用它，不能用 wxid）。

        只在主线程调用（collect / 启动补跑），复用 self.db；不用 AI 线程的实例。
        """
        try:
            nm = self.db.get_nickname(conv_id) if self.db else ""
        except Exception:           # noqa: BLE001
            return ""
        nm = (nm or "").strip()
        return "" if not nm or nm == conv_id else nm

    def _has_real_message_after(self, conv_id, ts):
        """通过事件之后，对方有没有发过**真正的**消息（不是系统提示）。

        用来判断「还要不要主动打招呼」。读库失败按「没说过」处理——宁可多发
        一条，也别把该激活的人漏掉：漏发的代价是这个客户从此沉寂，而多发一条
        最多显得热情。
        """
        try:
            rows = monitor_store.get_messages(conv_id, limit=20, since_ts=ts)
        except Exception:           # noqa: BLE001
            return False
        for m in rows:
            if m.get("role") != "them":
                continue
            if (m.get("ts") or 0) <= ts:
                continue            # since_ts 是 >=，会把事件本身带进来
            t = m.get("text") or ""
            if any(p in t for p in FRIEND_PASS_PATTERNS):
                continue            # 通过提示本身不算「说过话」
            return True
        return False

    def _backfill_pass_events(self, hours=24):
        """启动补跑：把最近 N 小时内漏判的「好友通过」补上。

        为什么需要：通过事件原本只在消息**新入库那一刻**由 collect 触发。进程
        没开、或归属判定把事件吞了（旧版本正是这个 bug），这次通过就永久丢了
        ——客户明明通过了，却永远收不到第一条消息，看板也永远停在「已申请」。

        幂等：get_friend_sent 只返回 status='sent' 的，已 passed 的不会再被认领；
        按 ts 升序处理，保证串行认领的顺序与实时流程一致。
        """
        since = int(time.time()) - int(hours * 3600)
        try:
            rows = monitor_store.recent_messages_matching(
                FRIEND_PASS_PATTERNS, since, limit=200)
        except Exception as e:      # noqa: BLE001
            print("[auto] 补跑好友通过失败（不影响启动）: %s" % e)
            rows = []
        if rows:
            before = len(self._pass_seen)
            for r in rows:
                try:
                    self._maybe_friend_pass(
                        {"conv_id": r.get("conv_id"), "text": r.get("text"),
                         "ts": r.get("ts"), "name": None, "type": ""},
                        quiet=True)
                except Exception as e:  # noqa: BLE001
                    print("[auto] 补跑好友通过异常: %s" % e)
            n = len(self._pass_seen) - before
            if n:
                print("[auto] 启动补跑：处理了 %d 条历史好友通过事件（近 %d 小时）"
                      % (n, hours))

        # 第二步：已通过、但首条激活一条都没真正发出去的，补排一次。
        # 覆盖「上一版拿 wxid 当搜索词导致整批 failed」这种情况——不补的话，
        # 用户只能删好友重加才能再验证一次（2026-10-19 就踩了这坑）。
        # 上一步刚排过的不会重复：_first_touch 里已经有它。
        try:
            pend = monitor_store.get_friend_passed_pending_touch(limit=20)
        except Exception as e:      # noqa: BLE001
            print("[auto] 补排首条激活失败（不影响启动）: %s" % e)
            return
        n2 = 0
        for r in pend:
            cid = (r.get("conv_id") or "").strip()
            if not cid or cid in self._first_touch:
                continue
            try:
                if r.get("passed_at") and self._has_real_message_after(
                        cid, int(r["passed_at"])):
                    continue        # 客户自己已经开口了，别去打扰
            except Exception:       # noqa: BLE001
                pass
            name = r.get("name") or self._nick_of(cid) or cid
            base = time.time()
            if self._first_touch:
                base = max(base, max(f["due"] for f in self._first_touch.values()))
            d = random.uniform(self.ft_delay_min, self.ft_delay_max)
            self._first_touch[cid] = {
                "due": base + d,
                "name": name,
                "note": r.get("note") or "",
                "source": r.get("source") or "",
                "tid": r["id"],
            }
            n2 += 1
            print("[auto] 补排首条激活：%s，%.0fs 后" % (name, base + d - time.time()))
        if n2:
            print("[auto] 启动补跑：%d 条已通过但首条未发出的，已重新排期" % n2)

    def _cancel_first_touch(self, conv_id):
        ft = self._first_touch.pop(conv_id, None)
        if ft:
            print("[auto] 取消首条激活（客户已主动说话）：%s" % ft["name"])

    def _first_touch_intent(self):
        """首条激活的专用意图（不复用日常回复的 intent）。

        优先用 config.add_friend.first_touch_intent（显式话术，改文案不用动代码）；
        留空时退到下面这段内置兜底，身份/定位与 config.ai 同步。
        """
        if self.ft_intent:
            return self.ft_intent
        ai = self.cfg.get("ai") or {}
        persona = ai.get("persona") or "小光"
        company = ai.get("company") or "中照金智（广州）数据科技"
        product = (ai.get("product") or "AI照明工作站（A站）").strip()
        positioning = (ai.get("positioning") or "为设计提效的 AI SaaS 平台").strip()
        prod = ("，核心产品是「%s」" % product) if product else ""
        return (
            "你正在微信上给一位**刚通过好友验证、还没说过话**的联系人写第一条消息"
            "（自我介绍 + 破冰）。\n"
            "身份：你是「%s」，%s的在线顾问；公司做的是%s%s——"
            "帮照明/建筑/室内/景观设计师用 AI 出效果图和方案汇报物料。\n"
            "红线：① 我们卖的是 SaaS 平台/工具，**不是接设计项目的外包服务商**，"
            "绝不说“可以帮您做设计/接项目/代出方案”；② 不报价、不承诺效果、不催单；"
            "③ 不主动说自己是 AI。\n"
            "要求：①一句话自报身份（不念公司简介）；②说明来意（优先结合给出的"
            "来源/备注，没有就泛到行业，绝不编造对方信息）；"
            "③给一个具体、可感知的价值点，可以说“需要的话我发份介绍给您看看”；"
            "④不堆产品功能；⑤口语化、80 字以内，只输出正文。"
            % (persona, company, positioning, prod))

    def _tick_first_touch(self):
        """每秒检查一次：到点且未被取消的，投递一条 first_touch 任务。"""
        if not self._first_touch:
            return
        now = time.time()
        for conv_id in list(self._first_touch.keys()):
            ft = self._first_touch[conv_id]
            if now < ft["due"]:
                continue
            del self._first_touch[conv_id]
            if not self.switches.get(conv_id, False):
                print("[auto] 首条激活跳过（会话开关已关）：%s" % ft["name"])
                continue
            if not self._rate_ok(now):
                print("[auto] 首条激活跳过（每小时上限）：%s" % ft["name"])
                continue
            ctx = "（系统事件）对方刚刚通过好友申请，还没有发过任何消息。"
            if ft.get("source"):
                ctx += "来源：%s。" % ft["source"]
            if ft.get("note"):
                ctx += "背景：%s。" % ft["note"]
            task = ReplyTask(conv_id, ft["name"], ctx, self.task_timeout,
                             kind="first_touch")
            task.id = self._task_open(task)
            try:
                self._ai_q.put_nowait(task)
            except queue.Full:
                print("[auto] 待生成队列已满，首条激活放弃：%s" % ft["name"])
                continue
            self._hour_window.append(now)
            print("[auto] 首条激活投递 → %s" % ft["name"])

    # ---------- 流水线 1：AI 生成（串行，一条完成立刻接下一单） ----------
    def _ai_worker(self):
        """消费「待生成」队列：调大模型出文案 → 投递到「待发送」队列。

        这是用户要的关键点：**AI 请求完成一条就直接发下一条 AI 请求**，
        不等上一条消息发送完（发送由另一个线程独立消费）。
        """
        while not self._stop.is_set():
            try:
                task = self._ai_q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._generate(task)
            except Exception as e:      # noqa: BLE001 —— worker 绝不能因单条异常退出
                print("[auto] AI 环节异常(%s): %s" % (task.name, e))

    # ---------- 人工接管：客户有意向就通知真人、让出会话 ----------
    def _maybe_handoff(self, task):
        """返回 True = 这条已转人工，调用方不要再生成/发送自动回复。

        闸门顺序刻意从便宜到贵：开关 → 有没有配通知对象 → 是不是激活过的会话 →
        冷却期 → 最后才调模型做意向判定。任一步不满足就安静返回，绝不影响
        正常回复链路；`handoff` 整个模块出任何异常也都被吞在这里。
        """
        if task.kind != "reply":
            return False
        # 接管只针对 1 对 1 客户会话。群里一句 @ 就关掉整个群的自动回复、
        # 再往内部群发通知，动静太大且容易误判。
        if task.conv_id.endswith(GROUP_SUFFIX):
            return False
        try:
            conf = handoff.load(self.cfg)
            if not conf["enabled"] or not conf["targets"]:
                return False
            if not handoff.in_scope(task.conv_id, conf):
                return False
            if handoff.cooling(task.conv_id, conf):
                return False
            ok, reason = handoff.detect(self.cfg, conf, task.text)
            if not ok:
                return False

            name = (task.name or task.conv_id).strip()
            paused = bool(conf["pause_auto_reply"])
            text = handoff.notice(name, task.text, reason, paused)
            if paused:
                try:
                    monitor_store.set_switch(task.conv_id, False)
                    self.switches[task.conv_id] = False
                except Exception as e:      # noqa: BLE001
                    print("[handoff] 暂停自动回复失败（继续通知）: %s" % e)
                    paused = False

            # 通知走的是同一条发送队列：UI 独占门、三态回读校验、账本全都现成。
            # kind='handoff' 不进 count_outreach_since，不占主动触达额度。
            delivered = []
            for t in conf["targets"]:
                nt = ReplyTask(t["conv_id"], t["name"], "", self.task_timeout,
                               kind="handoff", by="handoff")
                nt.reply = text
                nt.id = self._task_open(nt)
                try:
                    self._send_q.put_nowait(nt)
                    delivered.append(t)
                except queue.Full:
                    self._task_set(nt, "failed", reason="接管通知队列已满")
                    print("[handoff] 待发送队列已满，没通知到 %s" % t["name"])
            handoff.log(task.conv_id, name, task.text, reason, delivered, paused)
            print("[handoff] %s 判定有意向（%s）→ 通知 %d 处，%s"
                  % (name, reason, len(delivered),
                     "已暂停自动回复" if paused else "自动回复保持开启"))
            self._task_set(task, "handoff", reason="已转人工接管：%s" % reason)
            # 一个通知都没投出去就不算接管成功，让机器人照常回客户，别晾着他
            return bool(delivered) and bool(conf["skip_ai_reply"])
        except Exception as e:          # noqa: BLE001 —— 接管绝不能拖垮回复主流程
            print("[handoff] 判定异常（按未命中处理）: %s" % e)
            return False

    def _generate(self, task):
        """一条任务的大模型环节：过拟人节流 → 调 AI → 投递到待发送队列。"""
        if task.expired():
            print("[auto] 任务超时未生成（预算 %.0fs 已耗尽）: %s"
                  % (self.task_timeout, task.name))
            self._task_set(task, "expired", reason="预算耗尽（未生成）")
            return
        # 意向识别放在 AI 生成之前：命中就直接转真人，不再生成机器人回复。
        # 只对客户主动发来的消息判定，主动触达/首条激活本身不进这个分支。
        if self._maybe_handoff(task):
            return
        self._task_set(task, "generating")     # 账本：AI 线程已接手
        # 拟人节流（wechatauto rhythm）：写动作前统一过闸——两次写动作最小间隔
        # + 突发窗口内 burst 次后强制 cooloff。与同会话冷却/每小时上限叠加取严
        # （各自管不同维度：会话级/小时级/全局节奏级）。状态落盘
        # ~/.wechatauto/rhythm.json，与 wechat_ui 手动外发共享节流计数。
        # 放在 AI 生成之前：等待发生在 UI 门外，不会占着微信前台。
        try:
            from wechatauto import rhythm as _rhythm
            if self.rhythm_profile and self.rhythm_profile != "off":
                _rhythm.set_profile(self.rhythm_profile)
            waited = _rhythm.gate("reply")
            if waited >= 1.0:
                print("[auto] 拟人节流等待 %.1fs 后生成" % waited)
        except ImportError:
            pass   # 库没装 rhythm 模块时静默跳过，不影响主流程
        except Exception as e:
            print("[auto] 拟人节流异常（已跳过，不影响发送）: %s" % e)
        try:
            if task.kind == "first_touch":
                intent = self._first_touch_intent()
            elif task.kind == "outreach":
                intent = self._build_outreach_intent(task.text, task.variant)
            else:
                intent = self._build_intent(task.conv_id)
            reply = ai_generate(
                self.cfg, self._contact_of(task.conv_id, task.name),
                intent,
                extra_context=task.text,
                # AI 自身也受任务预算约束：剩余不足就别再等满 60s
                timeout=max(5, min(60, task.remaining())),
            )
        except Exception as e:
            print("[auto] AI 生成失败(%s): %s" % (task.name, e))
            self._task_set(task, "failed", reason="AI 生成失败: %s" % e)
            return
        if not reply:
            self._task_set(task, "failed", reason="AI 返回空内容")
            return
        task.reply = reply
        self._task_set(task, "generated", reply=reply)   # 账本：待发送
        print("[auto] 生成回复 → %s: %s" % (task.name, reply))
        try:
            self._send_q.put_nowait(task)
        except queue.Full:
            print("[auto] 待发送队列已满(%d 条)，本条未发出: %s"
                  % (self.queue_max, task.name))
            self._task_set(task, "failed",
                           reason="待发送队列已满(%d)，未发出" % self.queue_max)

    # ---------- 发送结果校验（回读微信库，真实发出才算数） ----------
    def _send_db(self):
        """发送线程用的微信库（db_gate 进程单例）。

        库内部每次查询都是 ``_open(rel)`` 新开连接、用完 close；共享的只是
        WeChatDB 对象（密钥 / 私有缓存目录 / 构建锁注册表），不会跨线程共用
        sqlite 连接。反过来，多建实例会因实例级构建锁各解各的、并发
        os.replace 同一份副本，触发 WinError 5。
        """
        if self._send_db_obj is None:
            try:
                self._send_db_obj = db_gate.get_db()
            except Exception as e:          # noqa: BLE001
                print("[auto] 发送校验初始化 WeChatDB 失败（无法校验，不写库）: %s" % e)
                return None
        return self._send_db_obj

    def _confirm_sent(self, conv_id, text, timeout=None):
        """回读微信库，确认这条消息真的出现在会话里——确认到才允许写库。

        为什么不能只认「键盘序列没抛异常」（2026-10-19 实测事故）：微信是 Qt
        自绘界面，读不到输入框状态；Enter 按在一个**没聚焦的输入框**上时全程
        静默不报错，调用方于是把一条**根本没发出去**的消息记成已发送——看板
        有、微信客户端没有，用户以为发了。

        微信写库有延迟（实测 1~3s），所以轮询等待，默认最多
        `auto_reply.confirm_timeout_seconds`（6s）。

        返回**三态**（2026-10-19 用户口径修正：真实发出才落库）：
          True  = 微信库里查到了这条 → 可以写库；
          False = 查了，确认没有 → 判未发出（SendNotVerified）；
          None  = 读库本身不可用，根本没法判断。**既不写库也不重发**：
                  写库等于记一条没证据的消息；重发等于给客户发两遍
                  （发送不可逆，重复比漏记更糟）。交人工核对。
        """
        conv_id = conv_id or ""
        probe = (text or "").strip()[:24]
        if not conv_id or not probe:
            return True
        db = self._send_db()
        if db is None:
            return None
        budget = self.confirm_timeout if timeout is None else timeout
        deadline = time.time() + max(1.0, budget)
        # 判定「这条是不是我发的」只认 sender_username == 本机 wxid。
        # 旧代码用 sender_id == "2"：那是分片内 Name2Id 的 rowid，换设备/换账号
        # 后不是 2，回读永远找不到自己刚发的那条 → 一律判未发出 → 一直重发
        # （2026-11-06 换设备事故，与看板左右颠倒同一个根因）。
        me = me_username(db)
        while True:
            try:
                msgs = db.get_messages(conv_id, limit=6)
            except Exception as e:          # noqa: BLE001
                print("[auto] 发送校验读库失败（无法确认，不写库也不重发）: %s" % e)
                return None
            for m in msgs:
                if not is_self(m, me):
                    continue
                if probe in str(m.get("content") or ""):
                    return True
            if time.time() >= deadline:
                # 附上该会话最新几条：能区分「按键没进输入框」（会话尾部没变化）
                # 和「发到别处去了」（别处会出现这条）——排查时少猜一轮。
                try:
                    tail = ["%s:%s" % (m.get("sender_username") or m.get("sender_id"),
                                       str(m.get("content"))[:16])
                            for m in (msgs or [])[-3:]]
                    print("[auto] 校验超时，%s 最新 %d 条: %s"
                          % (conv_id, len(tail), " / ".join(tail)))
                except Exception:       # noqa: BLE001
                    pass
                return False
            time.sleep(1.0)

    # ---------- 流水线 2：发送（串行，在 UI 门内） ----------
    def _send_worker(self):
        """消费「待发送」队列：每条在 UI 门内按键盘流程发出（跨进程排队）。

        UI 排队上限 = 这条任务的**剩余预算**（不是全局 180s）——任务级超时的
        意思就是：这条任务只剩这么多时间，排不到就作废，记日志继续下一条。
        异常绝不冒泡出循环（worker 死了整条流水线就停了）。
        """
        while not self._stop.is_set():
            try:
                task = self._send_q.get(timeout=0.5)
            except queue.Empty:
                continue
            rem = task.remaining()
            if rem <= 0:
                print("[auto] 任务超时未发出（预算 %.0fs 已耗尽）: %s"
                      % (self.task_timeout, task.name))
                self._task_set(task, "expired", reason="预算耗尽（未发出）")
                self._campaign_result(task, False, "预算耗尽（未发出）")
                continue
            if self._send_q.qsize():
                print("[auto] 发送 %s（后面还排着 %d 条）" % (task.name, self._send_q.qsize()))
            self._task_set(task, "sending")    # 账本：发送线程已接手
            try:
                self.bot.send_to_contact(task.name, task.reply,
                                         conv_id=task.conv_id, ui_timeout=rem,
                                         by=task.by,   # ai / manual / handoff / campaign
                                         confirm=self._confirm_sent,
                                         at_name=task.at_name)   # 群聊：发送前先 @ 提问者
            except ui_gate.UITimeoutError as e:
                print("[auto] 发送排队超时（本条 %.0fs 预算内没排到）: %s —— %s"
                      % (task.name, rem, e))
                reason = "UI 排队超时（%.0fs 预算内没排到）" % rem
                self._task_set(task, "expired", reason=reason)
                self._campaign_result(task, False, reason)
                continue
            except SendNotVerified as e:
                # 键盘动作跑完了，但回读微信库没有这条 → 目标会话里没出现。
                # 这种**不重试**：发送不可逆，万一其实发了再补一次就是重复打扰客户。
                # 落 failed 让看板看得见，交人工决定。
                # 另外还要提示「可能发到别的会话了」——Ctrl+F 那一环万一失败，
                # 正文会落到当时打开的那个会话里，光看目标会话是查不到的。
                print("[auto] 发送未确认，按未发出处理(%s): %s" % (task.name, e))
                reason = ("发送未确认：目标会话里没查到这条（可能没发出，"
                          "也可能发到了别的会话，请人工核对）")
                self._task_set(task, "failed", reason=reason)
                self._campaign_result(task, False, reason)
                continue
            except SendUnconfirmed as e:
                # 校验链路本身不可用（读库打不开、微信刚重启）。这条消息**可能
                # 已经发出去了**，所以绝不重试；也不写库——真实发出才落库。
                # 落 failed 并写明「无法确认」，人工去客户端看一眼即可。
                print("[auto] 发送无法确认(%s): %s" % (task.name, e))
                reason = "无法确认：回读微信库不可用，可能已发出"
                self._task_set(task, "failed", reason=reason)
                self._campaign_result(task, False, reason)
                continue
            except SendAborted as e:
                # 没进到目标会话就中止了：**确定什么都没发出去**。
                # 与上面两种不同，这里不存在「可能已发出」的风险，面板上直接写
                # 清楚卡在哪一环（搜索没进 / 正文没粘进去 / 没切回聊天页）。
                print("[auto] 发送中止(%s): %s" % (task.name, e))
                reason = "未发送（按键前已中止）：%s" % str(e)[:110]
                self._task_set(task, "failed", reason=reason)
                self._campaign_result(task, False, reason)
                continue
            except Exception as e:      # noqa: BLE001
                print("[auto] 发送异常(%s): %s" % (task.name, e))
                reason = "发送异常: %s" % e
                self._task_set(task, "failed", reason=reason)
                self._campaign_result(task, False, reason)
                continue
            # 发送成功（dry-run 下 send_to_contact 不真发，账本注明，免得看板误判）
            self._task_set(task, "sent", reply=task.reply,
                           reason=("dry-run：未真发" if self.dry_run else None))
            self._campaign_result(task, True)
            # 主动触达发出去之后，顺手把该会话的自动回复打开：我们主动找的人
            # 回话了我们却不接，比不发还糟。dry-run 不真发，也就不该改开关。
            if task.kind == "outreach" and not self.dry_run:
                try:
                    monitor_store.set_switch(task.conv_id, True)
                    print("[auto] 已开启 %s 的自动回复（客户回话会有人接）" % task.name)
                except Exception as e:  # noqa: BLE001
                    print("[auto] 开启 %s 的自动回复失败（客户回话需人工看）: %s"
                          % (task.name, e))

    # ---------- 活动触达 worker（固定文案 + 指定时间 + 指定对象） ----------
    def _campaign_result(self, task, ok, reason=""):
        """把单个目标的发送结果回写到活动账本；普通任务直接跳过。"""
        if not task.campaign_id:
            return
        try:
            monitor_store.record_campaign_result(
                task.campaign_id, task.conv_id, task.name, ok, reason)
            # 释放「在途」名额：发送线程与活动线程各自跑，用同一把锁保护内存态
            with self._campaign_lock:
                st = self._campaign_active.get(task.campaign_id)
                if st:
                    st["queued"].discard(task.conv_id)
        except Exception as e:          # noqa: BLE001
            print("[campaign] 结果回写失败(%s): %s" % (task.name, e))

    def _campaign_worker(self):
        print("[auto] 活动触达线程已启动（每 20s 巡检到点的活动）")
        while not self._stop.is_set():
            try:
                self._campaign_tick()
            except Exception as e:      # noqa: BLE001 —— worker 绝不能因单条异常退出
                print("[campaign] 本轮异常（已跳过）: %s" % e)
            self._stop.wait(20)

    def _campaign_tick(self):
        """一轮巡检：收新到点的活动 → 每个活动本轮最多投 1 个对象。

        为什么一次只投 1 条：活动常常一次选几十个对象，若一股脑塞进发送队列，
        客户此时发消息要排在几十条后面才能回，等于把自动回复堵死了。20s 一条
        既让活动在几分钟内发完，也给回复留出了插队空间。
        """
        now = int(time.time())
        for c in monitor_store.due_campaigns(now):
            cid = c["id"]
            if cid in self._campaign_active:
                continue
            if not monitor_store.claim_campaign(cid):
                continue                # 别的进程/上一轮已经抢走了
            try:
                targets = json.loads(c.get("targets") or "[]")
            except Exception:           # noqa: BLE001
                targets = []
            content = (c.get("content") or "").strip()
            if not content or not targets:
                monitor_store.record_campaign_result(
                    cid, "", "", False, "内容为空或没选对象")
                continue
            with self._campaign_lock:
                self._campaign_active[cid] = {
                    "content": content, "targets": targets,
                    "ids": [t.get("conv_id") for t in targets],
                    "queued": set(),
                }
            print("[campaign] 开始执行「%s」（%d 个对象）"
                  % (c.get("name") or cid, len(targets)))

        for cid, st in list(self._campaign_active.items()):
            db = monitor_store.get_campaign(cid)
            if not db or db["status"] in ("sent", "partial", "expired", "canceled"):
                with self._campaign_lock:
                    self._campaign_active.pop(cid, None)
                continue
            with self._campaign_lock:
                # 同一活动始终只保留 1 条在途：几十个对象也不会一次塞满发送队列
                if st["queued"]:
                    continue
                done = {r.get("conv_id") for r in db["result"]}
                nxt = next((t for t in st["targets"]
                            if t.get("conv_id") not in done), None)
                if nxt is None:
                    if all(i in done for i in st["ids"]):
                        self._campaign_active.pop(cid, None)
                    continue
                cid2 = nxt.get("conv_id")
                st["queued"].add(cid2)   # 先占名额，投递失败再释放
            name = (nxt.get("name") or cid2).strip()
            task = ReplyTask(cid2, name, "", max(self.task_timeout, 300),
                             kind="campaign", by="campaign", campaign_id=cid)
            task.reply = st["content"]
            task.id = self._task_open(task)
            try:
                self._send_q.put_nowait(task)
                print("[campaign] 投递 → %s" % name)
            except queue.Full:
                self._task_set(task, "failed", reason="活动触达队列已满")
                self._campaign_result(task, False, "发送队列已满")

    # ---------- 加好友 worker（名单队列，发完即走） ----------
    # 关键语义：**添加好友是异步的**。点完「确定」这条就结束，worker 立刻处理
    # 下一条，绝不等待对方通过。所以「未通过」不是失败、不占队列、不阻塞。
    def _in_work_hours(self, now=None):
        import datetime
        d = datetime.datetime.fromtimestamp(now or time.time())
        return self.af_work_start <= d.hour < self.af_work_end

    def _af_daily_limit(self):
        """当天额度：周末按 weekend_factor 折算（10 条 × 0.5 = 5 条）。"""
        import datetime
        if datetime.date.today().weekday() >= 5:
            return max(1, int(round(self.af_daily_limit * self.af_weekend_factor)))
        return self.af_daily_limit

    def _add_worker(self):
        while not self._stop.is_set():
            try:
                self._add_step()
            except Exception as e:      # noqa: BLE001
                print("[auto] 加好友 worker 异常（已跳过本轮）: %s" % e)
            self._stop.wait(20)

    def _add_enabled(self):
        """生效开关：看板设置（app_setting）优先，没有则用 config。

        为什么不用 config.json 直接改：auto_reply 只在启动时读一次配置，
        看板上点「关」要等重启才停，等于没有紧急刹车。走库的话 20 秒内生效。
        """
        try:
            v = monitor_store.get_setting("add_friend_enabled")
        except Exception:           # noqa: BLE001
            v = None
        if v is None or v == "":
            return self.af_enabled
        return (v == "1")

    def _add_step(self):
        """一次决策：能不能发，发谁。所有闸门都在这里判。"""
        if not self._add_enabled():
            return
        now = time.time()
        day = time.strftime("%Y-%m-%d")
        if self._af_paused_day == day:
            return
        if not self._in_work_hours(now):
            return
        # 间隔闸：上次尝试之后，随机 8~15 分钟才允许下一次（失败也计入，免得
        # 一条失败的记录每 20 秒被重试一次）
        if self._af_last_at:
            if now - self._af_last_at < random.uniform(self.af_gap_min,
                                                       self.af_gap_max):
                return
        limit = self._af_daily_limit()
        try:
            done = monitor_store.friend_sent_today()
        except Exception as e:          # noqa: BLE001
            print("[auto] 读今日加好友计数失败: %s" % e)
            return
        if done >= limit:
            if not getattr(self, "_af_limit_logged", False):
                print("[auto] 今日加好友已达上限 %d 条，明天继续" % limit)
                self._af_limit_logged = True
            return
        try:
            rows = monitor_store.get_friend_pending(limit=1)
        except Exception as e:          # noqa: BLE001
            print("[auto] 读加好友队列失败: %s" % e)
            return
        if not rows:
            return
        row = rows[0]
        tid, kw = row["id"], row["keyword"]
        print("[auto] 加好友开始：#%s %s（今日 %d/%d）" % (tid, kw, done, limit))
        try:
            import friend_add
            res = friend_add.add_friend(kw)
        except Exception as e:          # noqa: BLE001
            res = {"status": "failed", "reason": "模块异常: %s" % e}
        st = res.get("status")
        self._af_last_at = time.time()
        if st == "sent":
            monitor_store.update_friend_task(tid, status="sent",
                                             sent_at=int(time.time()), reason=None)
            self._af_fail_streak = 0
            self._af_limit_logged = False
            print("[auto] 加好友已发出：#%s %s（不等通过，继续下一条）" % (tid, kw))
        elif st == "already":
            monitor_store.update_friend_task(tid, status="already",
                                             reason=res.get("reason"))
            self._af_fail_streak = 0
            print("[auto] 跳过（对方已是好友）：#%s %s" % (tid, kw))
        else:
            tries = int(row.get("tries") or 0) + 1
            # 失败后放回 pending 再试；同一号码累计 3 次技术失败才落 failed
            status = "failed" if tries >= 3 else "pending"
            monitor_store.update_friend_task(tid, status=status,
                                             reason=res.get("reason"), tries=tries)
            self._af_fail_streak += 1
            print("[auto] 加好友失败：#%s %s —— %s（第 %d 次，连续 %d 次）"
                  % (tid, kw, res.get("reason"), tries, self._af_fail_streak))
            if self._af_fail_streak >= self.af_max_fail:
                self._af_paused_day = day
                print("[auto] 连续 %d 次加好友失败，今天停手"
                      "（大概率被限流/微信改版，继续点只会加重风控）"
                      % self._af_fail_streak)

    # ---------- 主动触达 worker（唤醒沉默客户，2026-10-21） ----------
    # 与自动回复的关系：**回复优先**。队列里还有活就先不主动发——客户找上来的
    # 时效性强，主动触达晚半小时没有任何代价。两者共用同一条 AI/发送流水线，
    # 所以 UI 门、三态确认、任务账本、看板全都是现成的。
    def _outreach_worker(self):
        print("[auto] 主动触达线程已启动（enabled=%s，每日上限 %d 条，%02d:00-%02d:00）"
              % (self._ou_enabled(), self.ou_daily_limit,
                 self.ou_work_start, self.ou_work_end))
        while not self._stop.is_set():
            try:
                self._outreach_tick()
            except Exception as e:      # noqa: BLE001 —— worker 绝不能因单条异常退出
                print("[auto] 主动触达异常（本轮跳过）: %s" % e)
            # 5 分钟一扫；用 wait 而不是 sleep，退出信号能立刻生效
            self._stop.wait(300)

    def _ou_enabled(self):
        """生效开关：看板/接口设置（app_setting）优先，没有则用 config。

        同 `_add_enabled` 的道理：config.json 只在启动时读一次，紧急刹车不能
        等重启。走库的话下一轮巡检（≤5 分钟）就生效。
        """
        try:
            v = monitor_store.get_setting("outreach_enabled")
        except Exception:           # noqa: BLE001
            v = None
        if v is None or v == "":
            return self.ou_enabled
        return (v == "1")

    def _outreach_tick(self):
        """一轮巡检：闸门全部通过才挑一个人发一条。任何一环不满足就安静退出。"""
        if not self._ou_enabled():
            return
        import datetime
        now = time.time()
        d = datetime.datetime.fromtimestamp(now)
        if not (self.ou_work_start <= d.hour < self.ou_work_end):
            return                      # 非工作时段（含夜间）一律不发
        # 回复优先：待生成/待发送队列非空，说明有客户在等，先让它们走完
        if not self._ai_q.empty() or not self._send_q.empty():
            return
        day_start = datetime.datetime(d.year, d.month, d.day).timestamp()
        try:
            sent_today = monitor_store.count_outreach_since(day_start)
        except Exception as e:          # noqa: BLE001
            print("[auto] 主动触达统计失败（本轮跳过）: %s" % e)
            return
        # daily_limit=0 表示不设每日上限，只靠条间 10~30 分钟 + 每会话冷却节流。
        if self.ou_daily_limit > 0 and sent_today >= self.ou_daily_limit:
            key = d.strftime("%Y-%m-%d")
            if self._ou_day != key:     # 一天只提示一次，别每 5 分钟刷一条
                self._ou_day = key
                print("[auto] 主动触达今日已达上限 %d 条，今天不再发" % self.ou_daily_limit)
            return
        if now < self._ou_next_at:
            return                      # 进程内间隔（投递时抽一次定死，不每轮重抽）
        try:
            last_at = monitor_store.last_outreach_ts()
        except Exception:               # noqa: BLE001
            last_at = 0
        # 跨重启也守住最小间隔：库里最近一条（含在途的）不足 gap_min 就不发
        if last_at and now - last_at < self.ou_gap_min:
            return
        # 可选全局周期闸：上次真实触达后 cycle_days 天内不再碰任何人
        if (self.ou_cycle_days > 0 and last_at
                and now - last_at < self.ou_cycle_days * 86400):
            return
        try:
            # 候选规则统一走 outreach_core（与 activate.py 同一份实现）：
            # 沉默客户在前、没聊过的破冰在后，冷却/累计次数都在那边卡。
            cands = oc.pick_targets(self.ou_cfg, limit=3, now=int(now))
        except Exception as e:          # noqa: BLE001
            print("[auto] 主动触达取候选失败（本轮跳过）: %s" % e)
            return
        for c in cands:
            conv_id = c.get("conv_id") or ""
            variant = c.get("kind") or "revive"
            if variant == "icebreak":
                if not self.ou_include_new:
                    continue            # 没显式授权，就不给没说过话的人发消息
                ctx = ""                # 破冰没有旧话题可承接，靠自我介绍开场
            else:
                ctx = self._outreach_context(conv_id)
                if not ctx:
                    continue            # 没有可承接的具体内容 → 跳过，不发空泛问候
            name = (c.get("name") or "").strip() or conv_id
            task = ReplyTask(conv_id, name, ctx, self.task_timeout, kind="outreach",
                             variant=variant)
            task.id = self._task_open(task)
            try:
                self._ai_q.put_nowait(task)
            except queue.Full:
                print("[auto] 待生成队列已满，本轮主动触达暂缓")
                return
            self._ou_next_at = now + random.uniform(self.ou_gap_min, self.ou_gap_max)
            if variant == "icebreak":
                print("[auto] 主动触达 → %s（加了好友没聊过，破冰；"
                      "下一条最早 %.0f 分钟后）"
                      % (name, (self._ou_next_at - now) / 60))
            else:
                days = int((now - (c.get("last_ts") or now)) / 86400)
                print("[auto] 主动触达 → %s（沉默 %d 天，最后一条是%s；"
                      "下一条最早 %.0f 分钟后）"
                      % (name, days,
                         "对方说的" if c.get("last_role") == "them" else "我方说的",
                         (self._ou_next_at - now) / 60))
            return

    def _outreach_context(self, conv_id):
        """拼「上次聊到哪」：最近几条真实消息 + 距今多少天。

        实现搬去了 outreach_core（activate.py 用的是同一份），这里只是按
        config 的 lookback_days 转发一下，避免两处规则慢慢走偏。
        """
        return oc.build_context(conv_id, lookback_days=self.ou_lookback_days,
                                max_lines=6)

    def _build_outreach_intent(self, ctx, variant="revive"):
        """主动触达的意图：把「主动 vs 回复」和「不许空泛问候」写死。

        不写死的话，AI 会把它当成一次普通回复，生成「最近怎么样」「在忙吗」
        这类废话——那既不像真人，也是最容易被当成营销号的行为。
        """
        if variant == "icebreak":
            return oc.icebreak_intent()
        return oc.revive_intent(ctx, self.ou_intent)

    # ---------- worker 启停 ----------
    def _start_workers(self):
        for fn, nm in ((self._ai_worker, "ai-worker"),
                       (self._send_worker, "send-worker"),
                       (self._moment_worker, "moment-worker"),
                       (self._add_worker, "add-friend-worker"),
                       (self._outreach_worker, "outreach-worker"),
                       (self._campaign_worker, "campaign-worker")):
            t = threading.Thread(target=self._worker_entry, args=(fn, nm),
                                 name=nm, daemon=True)
            t.start()
            self._threads.append(t)
        print("[auto] 流水线已启动：AI 生成 / 发送 / 朋友圈 / 加好友 / 主动触达 / "
              "活动触达 六个线程（生成与发送互不阻塞，朋友圈刷新也不再卡住收消息）")

    def _worker_entry(self, fn, name):
        """worker 统一入口：先给**本线程** CoInitialize（UIAutomation 前提），再跑活。

        不这么做，发送/朋友圈线程第一次碰 UIA 控件就报「尚未调用 CoInitialize」，
        那一次定位/切页会静默失败（详见 _thread_com_init 的说明）。
        初始化失败也照常跑——退化成加固前的行为，不让它把整条线程带崩。
        """
        holder = _thread_com_init(name)
        try:
            fn()
        finally:
            # holder 必须活到线程结束才 Uninitialize（它就是那个生命周期）
            if holder is not None:
                try:
                    holder.Uninitialize()
                except Exception:      # noqa: BLE001
                    pass

    def _stop_workers(self):
        self._stop.set()
        for t in self._threads:
            t.join(timeout=5)          # 让正在发送的那条尽量走完
        self._threads = []

    # ---------- 主循环 ----------
    def run(self):
        # 行缓冲：避免管道/部分终端环境下 print 被块缓冲憋到进程退出才输出
        try:
            sys.stdout.reconfigure(line_buffering=True)
        except Exception:
            pass
        try:
            self.db = db_gate.get_db()
            me = self.db.get_self_info()
            print("[auto] 当前账号: %s (%s)" % (me.get("nick_name"), me.get("username")))
        except Exception as e:
            print("[!] WeChatDB 初始化失败（微信需已登录并跑过 wechatauto 体检）: %s" % e)
            if getattr(e, "winerror", None) in (5, 32, 33):
                print("[!] 这是 Windows 文件占用错误。db_gate 已把解密副本隔离到"
                      "本进程私有目录；若仍复现，多半是共享目录残留，"
                      "关掉所有 python 进程后删除 "
                      r"%TEMP%\wechatauto_db\<账号>\ 下的 .tmp*/-shm/-wal 再启动。")
            sys.exit(1)
        self.switches = monitor_store.get_switches()
        # 启动清理：上次运行残留的未完成任务标为作废。内存队列随进程一起没了，
        # 库里那些 pending/generating 再也没人会动——不清理，看板会永远显示一堆
        # 假的"进行中"，「看得见」这件事本身就失真。不做补跑（旧回复已过时，
        # 补跑还可能和重启后新收的消息任务重叠，同一客户被回两次）。
        try:
            n_stale = monitor_store.expire_open_tasks()
            if n_stale:
                print("[auto] 上次运行残留 %d 条未完成任务，已标为作废（看板可查原因）"
                      % n_stale)
        except Exception as e:      # noqa: BLE001
            print("[auto] 清理历史任务失败（不影响启动）: %s" % e)
        try:
            n_af = monitor_store.expire_stale_friend_tasks(days=7)
            if n_af:
                print("[auto] %d 条 7 天未通过的好友申请已标为过期" % n_af)
        except Exception as e:      # noqa: BLE001
            print("[auto] 清理加好友队列失败（不影响启动）: %s" % e)
        # 补跑：进程停着的时候通过的好友，事件只在消息入库那一刻触发过一次，
        # 不补就永久丢了（客户通过了却永远收不到第一条）。必须在 self.switches
        # 加载之后调用——排首条要用它判会话开关。
        self._backfill_pass_events(hours=24)
        # __moment_dry__ 是朋友圈 dry-run 状态，复用 conv_switch 表存，不算会话
        on = [c for c, v in self.switches.items() if v and c != "__moment_dry__"]
        print("[auto] 库存统计: %s" % monitor_store.stats())
        if not self.enabled:
            print("[auto] 自动回复未启用（config.auto_reply.enabled=false）：仅收消息监控上屏，不回复任何人")
        elif not on:
            print("[auto] 没有任何会话打开自动回复开关：看板左侧会话列表每行右侧可逐个开（默认全关）")
        else:
            print("[auto] 已开启自动回复的会话 %d 个: %s" % (len(on), ", ".join(on[:10])))
        print("[auto] 自动回复启动 dry_run=%s 轮询间隔=%ds 合并窗口=%ds 单条任务预算=%ds（Ctrl+C 停止）"
              % (self.dry_run, self.interval, int(self.merge_window), self.task_timeout))
        if self._add_enabled():
            print("[auto] 加好友已启用：每日 %d 条（周末 ×%.1f）、间隔 %.0f~%.0f 分钟、"
                  "%02d:00-%02d:00（看板可随时开关，20 秒内生效）"
                  % (self.af_daily_limit, self.af_weekend_factor,
                     self.af_gap_min / 60.0, self.af_gap_max / 60.0,
                     self.af_work_start, self.af_work_end))
        else:
            print("[auto] 加好友未启用（config.add_friend.enabled=false 或看板已关）："
                  "名单只入库，不会自动发申请")
        if self._ou_enabled():
            limit_txt = ("%d 条" % self.ou_daily_limit) if self.ou_daily_limit > 0 else "不限"
            cycle_txt = ("%d 天一轮" % self.ou_cycle_days) if self.ou_cycle_days > 0 else "不限制"
            print("[auto] 主动触达已启用：每日 %s、间隔 %.0f~%.0f 分钟、%02d:00-%02d:00、"
                  "沉默 ≥%d 天、同一人冷却 %d 天/最多 %d 次、全局周期 %s；"
                  "没聊过的破冰=%s（看板可随时开关）"
                  % (limit_txt, self.ou_gap_min / 60.0, self.ou_gap_max / 60.0,
                     self.ou_work_start, self.ou_work_end,
                     self.ou_silent_days, self.ou_min_gap_days,
                     self.ou_max_per_contact, cycle_txt,
                     "开" if self.ou_include_new else "关"))
        else:
            print("[auto] 主动触达未启用（config.outreach.enabled=false）：不会主动给客户发消息")
        print("[auto] 首轮要全量解密全部数据库，可能数分钟；之后每轮秒级。"
              "扫描进度在终端单行刷新，日志里每轮只留一行汇总")
        # 朋友圈轮询（读路线：MomentDB 解密 sns.db，零 UI；写操作默认手动，
        # 开了客户自动开关（moment_switch）才对新圈自动点赞/评论）
        self._moment_ops = None
        self._start_workers()
        try:
            while True:
                try:
                    self.switches = monitor_store.get_switches()   # 每轮刷新，看板改开关下轮即生效
                    items = poll_new(self.db, progress=True)
                    for item in items:
                        self.collect(item)   # 只攒进合并缓冲，不建任务
                    if items:
                        print("[auto] 本轮扫描完成，新增 %d 条（待合并 %d / 待生成 %d / 待发送 %d）"
                              % (len(items), len(self._pending),
                                 self._ai_q.qsize(), self._send_q.qsize()))
                except Exception as e:
                    print("[auto] 轮询异常: %s" % e)
                # 把轮询间隔拆成 1s 小步：合并窗口要秒级到期，不能等整个 interval
                # 才检查一次（否则 interval=10s 时窗口会被拉成 10~18s）。
                for _ in range(max(1, int(self.interval))):
                    self._flush_due()
                    self._tick_first_touch()
                    time.sleep(1)
        except KeyboardInterrupt:
            print("[auto] 收到停止信号，收尾中…")
        finally:
            # KeyboardInterrupt 只会打在主线程；worker 是 daemon，别让它们被
            # 硬杀——先给正在发送的那条留点时间走完。
            self._stop_workers()
            print("[auto] 已停止")

    def _moment_worker(self):
        """朋友圈轮询线程：定期刷新 → 读库 → 按客户开关自动互动。

        为什么独立成线程（2026-10-16）：这一整套含 UI 刷新（带 4s 停留）和逐条
        点赞，一轮能占几十秒。留在主线程里会把「收到消息 → 立刻发起 AI 请求」
        整条流水线一起冻住，正好抵消本轮流水线改造的意义。
        节奏：每 `interval × 6` 秒一轮（与原来「每 6 轮约 60s」等效）。
        """
        period = max(10.0, float(self.interval) * 6)
        while not self._stop.is_set():
            try:
                self._poll_moments()
            except Exception as e:      # noqa: BLE001
                print("[auto] 朋友圈轮询异常（不影响主流程）: %s" % e)
            self._stop.wait(period)     # 可被停止信号立刻唤醒

    def _poll_moments(self):
        """朋友圈：定期 UI 刷新（点「刷新」按钮逼客户端同步）+ 水印增量读库 +
        按客户开关（moment_switch，默认全关）自动点赞/评论新圈。
        由 `_moment_worker` 线程按周期调用（2026-10-16 起不在主线程）。

        步骤设计（2026-10-16 二次重构，用户拍板）：
        - 刷新跑自己的定时（moment.refresh_interval），刷完**就地停在朋友圈页**，
          全程不做「切回聊天」——紧接着的自动互动本就要在朋友圈页定位；
          切回聊天再切回来不仅多余，且是当天自动点赞全失败的根因
          （find_moment 在聊天页白跑：库打「滚轮落点不属于微信 (0,0)」）。
        - 唯一的「回聊天页」发生在发消息前一步：send_to_contact 在 Ctrl+F
          之前自带 switch_to_chat_page（只有真收到聊天消息、要发消息时才走），
          所以微信停在朋友圈页不影响发消息，也没有多余操作。
        - 聊消息入库走解密库轮询（wx_recv_bridge），不需要手动刷新，互不依赖。
        """
        try:
            if self._moment_ops is None:
                import moment_ops
                self._moment_ops = moment_ops.MomentOps(self.cfg)
            # 定期 UI 刷新：微信 PC 只在用户点朋友圈页「刷新」按钮时才同步
            # 新圈，纯解密库读只能看到同步过的数据（2026-10-16 实测）。
            # interval=0 可关闭（只读库里已有的）。
            mcfg = self.cfg.get("moment", {}) or {}
            interval = float(mcfg.get("refresh_interval", 180))
            now = time.time()
            if interval > 0 and now - self._last_moment_refresh >= interval:
                self._last_moment_refresh = now
                if self._moment_ops.refresh():
                    print("[auto] 朋友圈: 已刷新（停在朋友圈页，紧接着读库）")
            stat = self._moment_ops.poll_new()
            if stat.get("new_moments") or stat.get("new_inter"):
                print("[auto] 朋友圈: 新圈 %d，新互动通知 %d"
                      % (stat.get("new_moments", 0), stat.get("new_inter", 0)))
            elif stat.get("error"):
                print("[auto] 朋友圈轮询: %s" % stat["error"])
            # 自动互动：只对本次新到的圈、按客户开关执行（dry-run 下只验证定位）。
            # 刚刷新过的话微信还在朋友圈页，定位无需再导航；没刷新过则由
            # _ensure_moments_page 兜底导航。做完就地留着，不回聊天页。
            astat = self._moment_ops.auto_interact()
            if astat.get("liked") or astat.get("commented"):
                print("[auto] 朋友圈自动互动: 点赞 %d，评论 %d（未执行 %d）"
                      % (astat["liked"], astat["commented"], astat["errors"]))
        except Exception as e:
            print("[auto] 朋友圈轮询异常（不影响主流程）: %s" % e)


def main():
    ap = argparse.ArgumentParser(description="微信自动回复闭环（收→AI→键盘发→看板）")
    ap.add_argument("--no-dry", action="store_true",
                    help="关闭 dry_run，真正发送（会抢占键鼠焦点）")
    ap.add_argument("--interval", type=int, default=None, help="轮询间隔秒数（覆盖 config）")
    args = ap.parse_args()

    cfg = load_config()
    ar = cfg.setdefault("auto_reply", {})
    if args.no_dry:
        ar["dry_run"] = False
    if args.interval:
        ar["interval_seconds"] = args.interval
    AutoReplier(cfg).run()


if __name__ == "__main__":
    main()
