#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
存量客户激活（activate.py）
================================
给**已经在通讯录里**的客户主动发一条消息，把话重新聊起来。

和 auto_reply 里的常驻「主动触达」区别：
  · 常驻触达是后台线程，按 config.json 自己跑，不用你敲命令（推荐日常用这个）；
  · 这里是**手动一次性**跑一遍，参数临时覆盖配置文件，适合「我现在就想发几条」。

**所有参数默认都从 config.json 的 `outreach` 块读**，所以最简单的用法就是不带
任何参数（`python activate.py` = 只列候选，零成本）。命令行参数只是临时覆盖，
不用每次手写 --limit。

两类候选、两种话术（实现在 outreach_core，与常驻触达共用同一份）：
  · revive   —— 聊过、现在沉默：承接上次聊到的**具体内容**（不许泛泛问候）
  · icebreak —— 加了好友没聊过：自我介绍 + 价值点 + 低压钩子

用法
----
    python activate.py                          # 只列候选，不调 AI、不发送（零成本）
    python activate.py --preview                # 生成文案给你看（会调 AI，不发）
    python activate.py --send                   # 按 config 的条数真发
    python activate.py --send --limit 3         # 临时只发 3 条
    python activate.py --send --icebreak        # 连「没聊过」的一起破冰

安全闸门（全部满足才发）
------------------------
  · 私聊：排除群、公众号、系统号、自己
  · 最后一条消息距今 >= outreach.silent_days（默认 7 天）
  · 同一人 outreach.min_gap_days 天内没被主动触达过（默认 14 天）
  · 累计触达次数 < outreach.max_per_contact（默认 3 次）
  · 当天全局上限 `outreach.daily_limit`（0=不设上限，靠条间间隔节流；
    与常驻触达共用同一份额度）
  · 单次最多 `outreach.per_run_limit` 条（防止一次把整个通讯录过一遍）
  · 只在 outreach.work_start_hour ~ work_end_hour 之间发（默认 8-21）
  · 两条之间随机等 outreach.interval_min/max_seconds（默认 10~30 分钟）
  · 发出去后**自动打开该会话的托管开关**（否则客户回你没人接），
    不想要这个行为就加 --no-switch

发送链路与自动回复完全一致：wechat_ui 的键盘流程 + UI 独占门 + 发后回读校验，
校验不过算失败、不写库、不重试（重复打扰客户比漏发一条更糟）。
"""
import argparse
import datetime
import random
import sys
import time

import monitor_store
import outreach_core as oc
import wechat_ui
import db_gate
from ai_client import generate as ai_generate
from wx_recv_bridge import is_self, me_username


def make_confirm(db, timeout=6.0):
    """发送结果回读校验：键盘动作跑完 ≠ 消息真发出去了。

    判「这条是不是我发的」用 sender_username == 本机 wxid（不能用 sender_id 编号，
    那是分片内的行号，换设备就变——详见 RUNBOOK 十）。
    返回 True=确认发出 / False=确认没有 / None=读库不可用（无法判断）。
    """
    me = me_username(db)

    def confirm(conv_id, text):
        probe = (text or "").strip()[:24]
        if not probe:
            return True
        deadline = time.time() + timeout
        while True:
            try:
                msgs = db.get_messages(conv_id, limit=6)
            except Exception:           # noqa: BLE001
                return None
            for m in msgs:
                if is_self(m, me) and probe in str(m.get("content") or ""):
                    return True
            if time.time() >= deadline:
                return False
            time.sleep(0.8)

    return confirm


def in_work_hours(start, end, now=None):
    h = (now or datetime.datetime.now()).hour
    if start <= end:
        return start <= h < end
    return h >= start or h < end       # 跨夜，例如 21~2


def main():
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:                   # noqa: BLE001
        pass

    cfg = wechat_ui.load_config()
    ou = (cfg.get("outreach") or {})

    def cget(key, fallback):
        v = ou.get(key, fallback)
        return fallback if v is None else v

    ap = argparse.ArgumentParser(
        description="存量客户激活：给通讯录里的客户主动发一条，把话聊起来"
                    "（参数默认全部取自 config.json 的 outreach 块）")
    ap.add_argument("--limit", type=int, default=None,
                    help="本次最多发几条（默认取 outreach.per_run_limit，单次上限）")
    ap.add_argument("--preview", action="store_true",
                    help="生成文案给你看，但不发送（会调 AI，有成本）")
    ap.add_argument("--send", action="store_true",
                    help="真正发送（不加这个参数一律只列候选/预览）")
    ap.add_argument("--icebreak", action="store_true", default=None,
                    help="把「加了好友从没聊过」的人也算进来"
                         "（默认取 outreach.include_new_contacts）")
    ap.add_argument("--silent-hours", type=float, default=None,
                    help="最后一条消息距今至少多少小时才碰"
                         "（默认 outreach.silent_days×24）")
    ap.add_argument("--max-days", type=int, default=None,
                    help="超过多少天没聊就不碰（默认 outreach.max_days，0=不限）")
    ap.add_argument("--min-gap-days", type=int, default=None,
                    help="同一人多少天内不重复激活（默认 outreach.min_gap_days）")
    ap.add_argument("--daily-limit", type=int, default=None,
                    help="当天全局上限（默认 outreach.daily_limit，0=不限）")
    ap.add_argument("--interval-min", type=float, default=None,
                    help="两条之间最短间隔秒（默认 outreach.interval_min_seconds）")
    ap.add_argument("--interval-max", type=float, default=None,
                    help="两条之间最长间隔秒（默认 outreach.interval_max_seconds）")
    ap.add_argument("--work-start", type=int, default=None,
                    help="可发送时段开始小时（默认 outreach.work_start_hour）")
    ap.add_argument("--work-end", type=int, default=None,
                    help="可发送时段结束小时（默认 outreach.work_end_hour）")
    ap.add_argument("--require-switch", action="store_true", default=None,
                    help="只处理已开启托管的会话（默认 outreach.require_switch）")
    ap.add_argument("--no-switch", action="store_true",
                    help="发完不要自动打开该会话的托管开关")
    args = ap.parse_args()

    if args.send and args.preview:
        ap.error("--preview 和 --send 只能选一个")

    # 取值优先级：命令行 > config.json > 代码兜底
    # 「单次跑多少条」和「每天最多几条」是两件事：日限关掉后仍需一个单次上限，
    # 免得手滑一次把整个通讯录过一遍。
    limit = (args.limit if args.limit is not None
             else int(cget("per_run_limit", 10)))
    silent_hours = (args.silent_hours if args.silent_hours is not None
                    else float(cget("silent_days", 7)) * 24)
    max_days = (args.max_days if args.max_days is not None
                else int(cget("max_days", 0) or 0))
    min_gap_days = (args.min_gap_days if args.min_gap_days is not None
                    else int(cget("min_gap_days", 14)))
    max_per_contact = int(cget("max_per_contact", 3))
    # 0 = 不设每日上限，只靠条间间隔 + 每会话冷却节流
    daily_limit = (args.daily_limit if args.daily_limit is not None
                   else int(cget("daily_limit", 0) or 0))
    gap_min = (args.interval_min if args.interval_min is not None
               else float(cget("interval_min_seconds", 600)))
    gap_max = (args.interval_max if args.interval_max is not None
               else float(cget("interval_max_seconds", 1800)))
    work_start = (args.work_start if args.work_start is not None
                  else int(cget("work_start_hour", 8)))
    work_end = (args.work_end if args.work_end is not None
                else int(cget("work_end_hour", 21)))
    icebreak = (args.icebreak if args.icebreak is not None
                else bool(cget("include_new_contacts", False)))
    require_switch = (args.require_switch if args.require_switch is not None
                      else bool(cget("require_switch", False)))

    targets = monitor_store.get_activation_targets(
        silent_hours=silent_hours, max_days=max_days, min_gap_days=min_gap_days,
        max_per_contact=max_per_contact,
        include_no_chat=icebreak, require_switch=require_switch,
        limit=max(10, limit * 4))       # 多取些：没上下文/生成失败的会被跳过

    revive = [t for t in targets if t["kind"] == "revive"]
    ice = [t for t in targets if t["kind"] == "icebreak"]
    print("[候选] 共 %d 个（沉默可唤回 %d / 未聊过可破冰 %d）"
          % (len(targets), len(revive), len(ice)))

    if not args.preview and not args.send:
        shown = targets[:limit]
        if len(shown) < len(targets):
            print("下面只列前 %d 个（--limit 可调，本次上限 %d 条）"
                  % (len(shown), limit))
        for t in shown:
            if t["kind"] == "revive":
                days = int((time.time() - (t["last_ts"] or time.time())) / 86400)
                tail = "最后一条是%s" % ("对方说的" if t["last_role"] == "them"
                                        else "我方说的")
                print("  · [唤回] %s（沉默 %d 天，%s）" % (t["name"], days, tail))
            else:
                print("  · [破冰] %s（没聊过）" % t["name"])
        print("\n这只是候选列表。加 --preview 看生成的话术（会调 AI），"
              "加 --send 才会真发。")
        return

    if not in_work_hours(work_start, work_end):
        print("[跳过] 现在不在可发送时段（%d:00~%d:00），避免半夜打扰客户"
              % (work_start, work_end))
        return

    day_start = int(datetime.datetime.now().replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp())
    try:
        used = monitor_store.count_outreach_since(day_start)
    except Exception as e:              # noqa: BLE001
        print("[跳过] 读取今日用量失败：%s" % e)
        return
    budget = (max(0, daily_limit - used) if daily_limit > 0
              else float("inf"))         # 0 = 不限，交给条间间隔与冷却控制节奏
    if budget <= 0:
        print("[跳过] 今日主动消息已达上限 %d 条（已用 %d）"
              % (daily_limit, used))
        return
    quota = min(limit, budget)

    db = db_gate.get_db()
    bot = wechat_ui.WeChatPCBot(cfg)
    bot.dry_run = not args.send          # 只有 --send 才解除演练
    confirm = make_confirm(db)

    sent = failed = previewed = 0
    for t in targets:
        if (sent + previewed) >= quota:
            break
        cid, name, variant = t["conv_id"], t["name"], t["kind"]
        if variant == "revive":
            ctx = oc.build_context(cid,
                                   lookback_days=int(cget("lookback_days", 180)))
            if not ctx:
                print("  [跳过] %s：没有可承接的聊天内容（不发空泛问候）" % name)
                continue
            intent = oc.revive_intent(ctx, cget("intent", ""))
        else:
            ctx = ""
            intent = oc.icebreak_intent(cfg)

        contact = {"name": name, "tags": [], "note": ""}
        try:
            text = ai_generate(cfg, contact, intent, extra_context=ctx, timeout=60)
        except Exception as e:          # noqa: BLE001
            print("  [失败] %s：AI 生成异常 %s" % (name, e))
            failed += 1
            continue
        if not text:
            print("  [跳过] %s：AI 返回空内容" % name)
            failed += 1
            continue

        print("\n→ %s（%s）\n  %s"
              % (name, "唤回" if variant == "revive" else "破冰", text))
        if not args.send:
            previewed += 1
            continue                    # preview 模式：只展示，不写库不发送

        # 任务账本：先落 pending，再发（与常驻触达同口径，这样「今日触达数」
        # 「同人冷却」两边都算得到，不会互相踩）
        tid = monitor_store.add_task(cid, name, ctx or "", int(time.time()) + 120,
                                     kind="outreach")
        try:
            bot.send_to_contact(name, text, conv_id=cid, confirm=confirm, by="ai")
        except wechat_ui.SendUnconfirmed as e:
            monitor_store.update_task(tid, status="failed", reason="无法确认：%s" % e)
            print("  [失败] %s：回读库不可用，没法确认是否发出（交人工核对）" % name)
            failed += 1
            continue
        except wechat_ui.SendNotVerified as e:
            monitor_store.update_task(tid, status="failed", reason="未确认到：%s" % e)
            print("  [失败] %s：微信库里没查到这条，按未发出处理" % name)
            failed += 1
            continue
        except wechat_ui.SendAborted as e:
            monitor_store.update_task(tid, status="failed", reason="发送中止：%s" % e)
            print("  [失败] %s：没进会话就中止（确定没发出）" % name)
            failed += 1
            continue
        except Exception as e:          # noqa: BLE001
            monitor_store.update_task(tid, status="failed", reason=str(e))
            print("  [失败] %s：%s" % (name, e))
            failed += 1
            continue

        monitor_store.update_task(tid, status="sent", reply=text,
                                  reason="activate.py:%s" % variant)
        # 消息库由 wechat_ui 内部写（已含 sent_by='ai'），这里不重复写
        sent += 1
        if not args.no_switch:
            try:
                monitor_store.set_switch(cid, True)
                print("  [已开托管] 客户回复会由自动回复接手")
            except Exception as e:      # noqa: BLE001
                print("  [warn] 打开托管开关失败（客户回复需人工看）：%s" % e)

        if sent < quota:
            wait = random.uniform(gap_min, gap_max)
            print("  … 等 %.0f 秒再发下一条" % wait)
            time.sleep(wait)

    if args.send:
        quota_txt = ("%d" % daily_limit) if daily_limit > 0 else "不限"
        print("\n[完成] 发出 %d 条，失败 %d 条（今日已用 %d，上限 %s）"
              % (sent, failed, used + sent, quota_txt))
    else:
        print("\n[预览] 生成 %d 条，**未发送**（确认话术没问题再加 --send）"
              % previewed)


if __name__ == "__main__":
    main()
