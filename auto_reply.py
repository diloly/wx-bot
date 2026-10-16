#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
auto_reply.py —— 自动回复闭环：实时收消息 -> 大模型生成 -> 键盘发送 -> 写回看板
=================================================================================
链路（三级流水线，2026-10-16 起）：
    1. 收：wx_recv_bridge.poll_new() 轮询 WeChatDB（本地 SQLCipher 解密，只读）
    2. 判：类型过滤(默认仅文本)/每会话开关(默认全关)/群开关/白名单/同会话冷却/
       每小时上限 —— 全部在**投递这一刻**判完，通过则建一条「回复任务」入队
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
    日志——这就是「对单条任务做限制」的落点。同一用户连发多条时，第一条建任务
    的瞬间就更新冷却窗口，后面的自然被同会话冷却挡掉（每个用户算一条）。

⚠️ 前提与风险：
- **总开关 `config.auto_reply.enabled`**：false 时本进程只收消息供看板监控、不回复任何人。
- 键盘发送会抢占键鼠焦点，运行时不要人工操作电脑（鼠标甩到左上角可 FAILSAFE 急停）。
- dry_run 默认 true：只打印「将回复什么」，不真发。确认无误后 --no-dry 真发。
- 频率受 max_replies_per_hour / cooldown_seconds 双闸限制，防行为风控。
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
    __slots__ = ("conv_id", "name", "text", "created", "deadline", "reply")

    def __init__(self, conv_id, name, text, timeout):
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
        self.cooldown = ar.get("cooldown_seconds", 30)
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
        self._last_reply = {}   # conv_id -> 上次建任务的时间戳（冷却窗口从建任务起算）
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
        """群/白名单闸（会话开关闸在 submit() 里单独判，静默跳过）。"""
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
        # 备注比昵称更可能是客户对他的称呼（「马总」），喂给 AI 后称呼不会出错。
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

    # ---------- 投递：判定闸门 → 建任务 → 入队（立刻返回，不等 AI） ----------
    def submit(self, item):
        """判定 + 建任务 + 投递到「待生成」队列。**不等 AI、不等发送**。

        闸门与计数全部在**投递这一刻**判完（不放到发送成功后）：
        否则同一轮里同一用户连发 3 条会全部通过、排成 3 条任务，
        「每个用户算一条」就破了。在这里更新 _last_reply / _hour_window，
        同一轮的后两条会被同会话冷却/每小时上限自然挡掉。
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
        # 每会话自动回复开关（默认关；仅开关打开的会话才触发AI回复，静默跳过不刷屏）
        if not self.switches.get(conv_id, False):
            return
        now = time.time()
        if now - self._last_reply.get(conv_id, 0) < self.cooldown:
            print("[auto] %s 冷却中(%ds)，跳过" % (name, self.cooldown))
            return
        if not self._rate_ok(now):
            print("[auto] 已达每小时上限(%d条)，不再新建任务" % self.max_per_hour)
            return
        self._last_reply[conv_id] = now        # 冷却窗口从「建任务」起算
        self._hour_window.append(now)
        task = ReplyTask(conv_id, name, item.get("text", ""), self.task_timeout)
        try:
            self._ai_q.put_nowait(task)
        except queue.Full:
            print("[auto] 待生成队列已满(%d 条)，本条不回复: %s" % (self.queue_max, name))
            return
        print("[auto] 已受理 %s（预算 %.0fs，待生成队列 %d 条）: %s"
              % (name, self.task_timeout, self._ai_q.qsize(),
                 (item.get("text") or "")[:30].replace("\n", " ")))

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
            return
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
            return
        if not reply:
            return
        task.reply = reply
        print("[auto] 生成回复 → %s: %s" % (task.name, reply))
        try:
            self._send_q.put_nowait(task)
        except queue.Full:
            print("[auto] 待发送队列已满(%d 条)，本条未发出: %s"
                  % (self.queue_max, task.name))

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
                continue
            if self._send_q.qsize():
                print("[auto] 发送 %s（后面还排着 %d 条）" % (task.name, self._send_q.qsize()))
            try:
                self.bot.send_to_contact(task.name, task.reply,
                                         conv_id=task.conv_id, ui_timeout=rem)
            except ui_gate.UITimeoutError as e:
                print("[auto] 发送排队超时（本条 %.0fs 预算内没排到）: %s —— %s"
                      % (task.name, rem, e))
                continue
            except Exception as e:      # noqa: BLE001
                print("[auto] 发送异常(%s): %s" % (task.name, e))

    # ---------- worker 启停 ----------
    def _start_workers(self):
        for fn, nm in ((self._ai_worker, "ai-worker"),
                       (self._send_worker, "send-worker"),
                       (self._moment_worker, "moment-worker")):
            t = threading.Thread(target=fn, name=nm, daemon=True)
            t.start()
            self._threads.append(t)
        print("[auto] 流水线已启动：AI 生成 / 发送 / 朋友圈 三个线程"
              "（生成与发送互不阻塞，朋友圈刷新也不再卡住收消息）")

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
        # __moment_dry__ 是朋友圈 dry-run 状态，复用 conv_switch 表存，不算会话
        on = [c for c, v in self.switches.items() if v and c != "__moment_dry__"]
        print("[auto] 库存统计: %s" % monitor_store.stats())
        if not self.enabled:
            print("[auto] 自动回复未启用（config.auto_reply.enabled=false）：仅收消息监控上屏，不回复任何人")
        elif not on:
            print("[auto] 没有任何会话打开自动回复开关：看板左侧会话列表每行右侧可逐个开（默认全关）")
        else:
            print("[auto] 已开启自动回复的会话 %d 个: %s" % (len(on), ", ".join(on[:10])))
        print("[auto] 自动回复启动 dry_run=%s 间隔=%ds 单条任务预算=%ds（Ctrl+C 停止）"
              % (self.dry_run, self.interval, self.task_timeout))
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
                        self.submit(item)   # 只判定+入队就返回；AI 与发送在各自线程跑
                    if items:
                        print("[auto] 本轮扫描完成，新增 %d 条（待生成 %d / 待发送 %d）"
                              % (len(items), self._ai_q.qsize(), self._send_q.qsize()))
                except Exception as e:
                    print("[auto] 轮询异常: %s" % e)
                time.sleep(self.interval)
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
