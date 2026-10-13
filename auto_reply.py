#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
auto_reply.py —— 自动回复闭环：实时收消息 -> 大模型生成 -> 键盘发送 -> 写回看板
=================================================================================
链路：
    1. 收：wx_recv_bridge.poll_new() 轮询 WeChatDB（本地 SQLCipher 解密，只读，已验证）
    2. 判：类型过滤(默认仅文本)/群开关/白名单/同会话冷却/每小时上限
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
        self.context_turns = ar.get("context_turns", 6)
        self.reply_types = set(ar.get("reply_types", ["文本"]))
        self.enabled = ar.get("enabled", False)   # 总开关：false 时只监控上屏，不回复任何人
        self.dry_run = ar.get("dry_run", True)
        self.bot = WeChatPCBot({
            "dry_run": self.dry_run,
            "send_delay_seconds": cfg.get("send_delay_seconds", 5),
        })
        self._last_reply = {}   # conv_id -> 上次回复时间戳
        self._hour_window = []  # 最近一小时的回复时间戳
        self.db = None

    # ---------- 判定闸门 ----------
    def _is_allowed(self, conv_id, name):
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
        return {"name": name, "tags": [], "note": ""}

    def _build_intent(self, conv_id):
        """把最近几轮对话拼进 intent，让回复有语境（不隔空答话）。"""
        intent = self.intent
        try:
            ctx = monitor_store.get_messages(conv_id, limit=self.context_turns * 2)
        except Exception:
            ctx = []
        if ctx:
            lines = [
                "%s: %s" % ("我" if m["role"] == "me" else "对方", m["text"])
                for m in ctx[-(self.context_turns * 2):]
            ]
            intent = intent + "\n【最近对话，供参考语境】\n" + "\n".join(lines)
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
        now = time.time()
        if now - self._last_reply.get(conv_id, 0) < self.cooldown:
            print("[auto] %s 冷却中(%ds)，跳过" % (name, self.cooldown))
            return
        if not self._rate_ok(now):
            print("[auto] 已达每小时上限(%d条)，本轮不回复" % self.max_per_hour)
            return
        try:
            reply = ai_generate(
                self.cfg, self._contact_of(conv_id, name), self._build_intent(conv_id))
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
        try:
            self.db = WeChatDB()
            me = self.db.get_self_info()
            print("[auto] 当前账号: %s (%s)" % (me.get("nick_name"), me.get("username")))
        except Exception as e:
            print("[!] WeChatDB 初始化失败（微信需已登录并跑过 wechatauto 体检）: %s" % e)
            sys.exit(1)
        if not self.enabled:
            print("[auto] 自动回复未启用（config.auto_reply.enabled=false）：仅收消息监控上屏，不回复任何人")
        print("[auto] 自动回复启动 dry_run=%s 间隔=%ds（Ctrl+C 停止）"
              % (self.dry_run, self.interval))
        while True:
            try:
                for item in poll_new(self.db):
                    self.handle(item)
            except KeyboardInterrupt:
                print("[auto] 已停止")
                break
            except Exception as e:
                print("[auto] 轮询异常: %s" % e)
            time.sleep(self.interval)


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
