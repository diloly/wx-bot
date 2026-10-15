#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
auto_reply.py —— 自动回复闭环：实时收消息 -> 大模型生成 -> 键盘发送 -> 写回看板
=================================================================================
链路：
    1. 收：wx_recv_bridge.poll_new() 轮询 WeChatDB（本地 SQLCipher 解密，只读，已验证）
    2. 判：类型过滤(默认仅文本)/每会话开关(默认全关，看板会话列表右侧逐个开)/
       群开关/白名单/同会话冷却/每小时上限
    3. 想：ai_client.generate(cfg, contact, intent + 最近对话上下文)
    4. 发：wechat_ui.WeChatPCBot.send_to_contact()（键盘驱动，需微信前台）
    5. 记：发送成功后由 send_to_contact 写回 monitor_store(role=me)，看板即时可见

⚠️ 前提与风险：
- **总开关 `config.auto_reply.enabled`**：false 时本进程只收消息供看板监控、不回复任何人
  （当前配置即此状态；单聊测试满意、群控确认无误后再打开）。
- 键盘发送会抢占键鼠焦点，运行时不要人工操作电脑（鼠标甩到左上角可 FAILSAFE 急停）。
- dry_run 默认 true：只打印「将回复什么」，不真发。确认无误后 --no-dry 真发。
- 频率受 max_replies_per_hour / cooldown_seconds 双闸限制，防行为风控。
- 只维护已知客户；群消息默认不自动回（allow_groups=false，群 wxid 以 @chatroom 结尾）。
- 消息 type 是中文字符串（"文本"/"图片"/...），reply_types 按字符串配。
- 本脚本已内含收消息轮询，不要再同时跑 wx_recv_bridge.py（二选一）。

用法：
    python auto_reply.py                # dry-run 观察（推荐先跑这个）
    python auto_reply.py --no-dry       # 真发（会抢焦点，慎用）
    python auto_reply.py --interval 5   # 自定义轮询间隔秒数
"""
import argparse
import sys
import time

import monitor_store
from ai_client import generate as ai_generate
from wechat_ui import WeChatPCBot, load_config
from wx_recv_bridge import poll_new, WeChatDB

GROUP_SUFFIX = "@chatroom"


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
        self.bot = WeChatPCBot({
            "dry_run": self.dry_run,
            "send_delay_seconds": cfg.get("send_delay_seconds", 5),
        })
        self._last_reply = {}   # conv_id -> 上次回复时间戳
        self._hour_window = []  # 最近一小时的回复时间戳
        self.switches = {}      # conv_id -> bool：每会话AI自动回复开关（默认全关，monitor_store.conv_switch 表）
        self._contact_cache = {}  # conv_id -> contact dict：反查结果缓存，避免重复查库
        self.rhythm_profile = ar.get("rhythm", "natural")  # 拟人节流档位（natural/calm/fast/off）
        self.db = None

    # ---------- 判定闸门 ----------
    def _is_allowed(self, conv_id, name):
        """群/白名单闸（会话开关闸在 handle() 里单独判，静默跳过）。"""
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
            rows = self.db.search_contact(name) or []
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

    # ---------- 单条处理 ----------
    def handle(self, item):
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
            print("[auto] 已达每小时上限(%d条)，本轮不回复" % self.max_per_hour)
            return
        # 拟人节流（wechatauto rhythm）：写动作前统一过闸——两次写动作最小间隔
        # gap + 突发窗口内 burst 次后强制 cooloff。与上面的同会话冷却/每小时上限
        # 叠加取严（各自管不同维度：会话级/小时级/全局节奏级）。状态落盘
        # ~/.wechatauto/rhythm.json，与 wechat_ui 手动外发共享节流计数。
        try:
            from wechatauto import rhythm as _rhythm
            if self.rhythm_profile and self.rhythm_profile != "off":
                _rhythm.set_profile(self.rhythm_profile)
            waited = _rhythm.gate("reply")
            if waited >= 1.0:
                print("[auto] 拟人节流等待 %.1fs 后发送" % waited)
        except ImportError:
            pass   # 库没装 rhythm 模块时静默跳过，不影响主流程
        except Exception as e:
            print("[auto] 拟人节流异常（已跳过，不影响发送）: %s" % e)
        try:
            reply = ai_generate(
                self.cfg, self._contact_of(conv_id, name), self._build_intent(conv_id),
                extra_context=item.get("text", ""))
        except Exception as e:
            print("[auto] AI 生成失败(%s): %s" % (name, e))
            return
        if not reply:
            return
        print("[auto] 生成回复 → %s: %s" % (name, reply))
        self.bot.send_to_contact(name, reply, conv_id=conv_id)
        self._last_reply[conv_id] = time.time()
        self._hour_window.append(time.time())

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
        print("[auto] 自动回复启动 dry_run=%s 间隔=%ds（Ctrl+C 停止）"
              % (self.dry_run, self.interval))
        print("[auto] 首轮要全量解密全部数据库，可能数分钟；之后每轮秒级。进度见 [scan] 行")
        # 朋友圈轮询（读路线：MomentDB 解密 sns.db，零 UI；写操作只从看板手动触发）
        self._moment_ops = None
        self._moment_round = 0
        _moment_every = 6          # 每 6 轮（约 60s）拉一次朋友圈，读 DB 成本低
        while True:
            try:
                self.switches = monitor_store.get_switches()   # 每轮刷新，看板改开关下轮即生效
                items = poll_new(self.db, progress=True)
                for item in items:
                    self.handle(item)
                if items:
                    print("[auto] 本轮扫描完成，新增 %d 条" % len(items))
                self._moment_round += 1
                if self._moment_round % _moment_every == 0:
                    self._poll_moments()
            except KeyboardInterrupt:
                print("[auto] 已停止")
                break
            except Exception as e:
                print("[auto] 轮询异常: %s" % e)
            time.sleep(self.interval)

    def _poll_moments(self):
        """朋友圈水印增量：关注客户新圈 + 未读互动落库（看板展示/手动点赞评论）。"""
        try:
            if self._moment_ops is None:
                import moment_ops
                self._moment_ops = moment_ops.MomentOps(self.cfg)
            stat = self._moment_ops.poll_new()
            if stat.get("followed") or stat.get("new_inter"):
                print("[auto] 朋友圈: 关注客户新圈 %d，新互动通知 %d"
                      % (stat.get("followed", 0), stat.get("new_inter", 0)))
            elif stat.get("error"):
                print("[auto] 朋友圈轮询: %s" % stat["error"])
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
