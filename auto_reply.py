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
import queue
import sys
import threading
import time

import monitor_store
import ui_gate
from ai_client import generate as ai_generate
from wechat_ui import WeChatPCBot, load_config
from wx_recv_bridge import poll_new, WeChatDB

GROUP_SUFFIX = "@chatroom"


class ReplyTask:
    """一条「回复任务」：从投递到消息发出算一条，粒度＝一个用户的一次回复。

    deadline 在**投递时**就定死（= 投递时刻 + task_timeout），所以「排队等候」
    也算在预算里——这正是用户要的「对单条任务做限制」：一条任务不能因为前面
    排了别人就无限期挂着。
    """
    __slots__ = ("id", "conv_id", "name", "text", "created", "deadline", "reply")

    def __init__(self, conv_id, name, text, timeout, task_id=None):
        self.id = task_id          # reply_task 表主键（落库后回填；None=未落库）
        self.conv_id = conv_id
        self.name = name
        self.text = text            # 触发本任务的客户原话（AI 的 extra_context）
        self.created = time.time()
        self.deadline = self.created + float(timeout)
        self.reply = None           # AI 生成后回填

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
        self.db = None                    # 主线程用（收消息轮询）
        self._ai_db_obj = None            # AI 线程用（联系人反查），避免跨线程共享连接
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

    def _contact_of(self, conv_id, name):
        for c in self.cfg.get("contacts", []):
            if c.get("name") == name or c.get("wxid") == conv_id:
                return c
        # config 没配这个联系人：用库反查补全（wxid 精确匹配 + 微信备注进 note）。
        # 备注比昵称更可能是客户对他的称呼（「某总」），喂给 AI 后称呼不会出错。
        cached = self._contact_cache.get(conv_id)
        if cached is not None:
            return cached
        info = {"name": name, "tags": [], "note": ""}
        try:
            # 本方法只被 AI 线程调用；用独立的 WeChatDB 实例，避免与主线程的
            # 收消息轮询共享同一个连接（跨线程共用连接是隐患）。
            if self._ai_db_obj is None:
                self._ai_db_obj = WeChatDB()
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
                kind="reply", created=task.created)
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
        if not self.enabled:
            return
        if item.get("role") == "me":
            return  # 自己发的消息不触发回复（防自问自答循环）
        conv_id, name = item["conv_id"], item["name"]
        if item.get("type") not in self.reply_types:
            print("[auto] 跳过非回复类型(%s): %s" % (item.get("type"), name))
            return
        if not self._is_allowed(conv_id, name):
            print("[auto] 跳过(群/名单外): %s" % name)
            return
        # 每会话自动回复开关（默认关；没开的会话静默不进缓冲，不刷屏）
        if not self.switches.get(conv_id, False):
            return
        now = time.time()
        buf = self._pending.get(conv_id)
        if buf is None:
            buf = {"name": name, "texts": [], "first_ts": now, "last_ts": now}
            self._pending[conv_id] = buf
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
        task = ReplyTask(conv_id, name, merged, self.task_timeout)
        task.id = self._task_open(task)        # 状态账本（看板可观测）
        try:
            self._ai_q.put_nowait(task)
        except queue.Full:
            print("[auto] 待生成队列已满(%d 条)，本批不回复: %s" % (self.queue_max, name))
            return
        print("[auto] 合并 %d 条为一条任务 → %s（预算 %.0fs，待生成队列 %d 条）"
              % (len(texts), name, self.task_timeout, self._ai_q.qsize()))

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

    def _generate(self, task):
        """一条任务的大模型环节：过拟人节流 → 调 AI → 投递到待发送队列。"""
        if task.expired():
            print("[auto] 任务超时未生成（预算 %.0fs 已耗尽）: %s"
                  % (self.task_timeout, task.name))
            self._task_set(task, "expired", reason="预算耗尽（未生成）")
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
            reply = ai_generate(
                self.cfg, self._contact_of(task.conv_id, task.name),
                self._build_intent(task.conv_id),
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
                continue
            if self._send_q.qsize():
                print("[auto] 发送 %s（后面还排着 %d 条）" % (task.name, self._send_q.qsize()))
            self._task_set(task, "sending")    # 账本：发送线程已接手
            try:
                self.bot.send_to_contact(task.name, task.reply,
                                         conv_id=task.conv_id, ui_timeout=rem)
            except ui_gate.UITimeoutError as e:
                print("[auto] 发送排队超时（本条 %.0fs 预算内没排到）: %s —— %s"
                      % (task.name, rem, e))
                self._task_set(task, "expired",
                               reason="UI 排队超时（%.0fs 预算内没排到）" % rem)
                continue
            except Exception as e:      # noqa: BLE001
                print("[auto] 发送异常(%s): %s" % (task.name, e))
                self._task_set(task, "failed", reason="发送异常: %s" % e)
                continue
            # 发送成功（dry-run 下 send_to_contact 不真发，账本注明，免得看板误判）
            self._task_set(task, "sent", reply=task.reply,
                           reason=("dry-run：未真发" if self.dry_run else None))

    # ---------- worker 启停 ----------
    def _start_workers(self):
        for fn, nm in ((self._ai_worker, "ai-worker"),
                       (self._send_worker, "send-worker"),
                       (self._moment_worker, "moment-worker")):
            t = threading.Thread(target=self._worker_entry, args=(fn, nm),
                                 name=nm, daemon=True)
            t.start()
            self._threads.append(t)
        print("[auto] 流水线已启动：AI 生成 / 发送 / 朋友圈 三个线程"
              "（生成与发送互不阻塞，朋友圈刷新也不再卡住收消息）")

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
            self.db = WeChatDB()
            me = self.db.get_self_info()
            print("[auto] 当前账号: %s (%s)" % (me.get("nick_name"), me.get("username")))
        except Exception as e:
            print("[!] WeChatDB 初始化失败（微信需已登录并跑过 wechatauto 体检）: %s" % e)
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
        print("[auto] 首轮要全量解密全部数据库，可能数分钟；之后每轮秒级。进度见 [scan] 行")
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
