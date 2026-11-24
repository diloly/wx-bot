#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回刷转发活动的错误结果记录（2026-11-24）。

问题背景
--------
旧版 ``send_forward_batch`` 在拼结果时把字典型 ``missing`` 元素塞进 ``set()``，
抛 ``unhashable type: 'dict'``，被 worker 捕获后把**整批**记成「转发异常」——
而消息其实已经发出去了。另一类「未列入转发结果」是右侧已选栏（虚拟列表）
只读得到可视区几行、拿它当发送名单导致的漏记。

本脚本按**真实证据**回刷，不靠猜：
    用 conv_id 直查微信库，在「这条结果自己记录时间的前后窗口」里找
    **我发出的**、内容含卡片标题的消息：
      有且 server_id>0 -> ok=True  「已转发（回刷）」
      有但 server_id=0 -> ok=False 「未送达（红色叹号/对方已删除或拒收）」
      有但读不到回执   -> ok=False 「无法确认送达」
      没有             -> ok=False 「已发出但回读未确认」（不谎报成功）
    再重算活动 status（全成功 sent / 否则 partial）。

用法
----
    python repair_campaign_results.py                  # 只统计，不改库（默认）
    python repair_campaign_results.py --apply          # 写回 result/status
    python repair_campaign_results.py --ids 23,25 --apply
    python repair_campaign_results.py --apply --include-ok   # 连正常的也重算一遍
    python repair_campaign_results.py --debug               # 打印命中消息的 server_id 证据

注意
----
* 默认只处理 reason 里带「转发异常 / unhashable type / 未列入转发结果」的条目。
* 回读依赖微信解密库，须与 auto_reply 在同一台机器、同一登录会话下运行；
  读不到时如实标「无法回读」，不会当成成功。
"""
import argparse
import json
import os
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import db_gate          # noqa: E402
import monitor_store    # noqa: E402

# 需要回刷的异常文案特征
BAD_MARKS = ("unhashable type", "未列入转发结果", "转发异常")
TS_TOLERANCE = 120      # 发送时间容差（秒）：一批发送可能跨几分钟
LOOKBACK = 3600         # 最多往记录时间之前找 1 小时（覆盖整批发送的耗时）
READ_LIMIT = 100        # 每页回读多少条
READ_MAX_PAGES = 6      # 最多翻几页（600 条）：活跃群在活动后可能又攒了几百条，
                        # 只取最近 100 条会误判成「没发到」


def _is_bad(reason):
    r = str(reason or "")
    return any(m in r for m in BAD_MARKS)


def _server_id_of(db, conv_id, row):
    """get_messages 不带 server_id；回原始行读。>0=送达，0=红色叹号，None=读不到。"""
    try:
        raw = db.get_message_row(conv_id, int(row["local_id"]), None,
                                 row.get("shard"))
        if not raw:
            return None
        return int(raw.get("server_id") or 0)
    except Exception:
        return None


def _landed(db, conv_id, since_ts, match, until_ts=0, me="", detail=None):
    """[since_ts, until_ts] 内找「我发的、带标题」的消息，并判它是否真送达。

    返回 delivered / undelivered / unknown / not-found / unreadable。

    * 只看「本地有没有这条消息」不够：对方把我删了时，微信**仍会留一条消息**
      （卡片也在）但 server_id=0，界面是红色叹号，实际没送达（2026-11-24 实测）。
    * ``me`` 有值时只认我发的——别人转发同一条卡片进来不算我发出去了。
    * ``until_ts`` 有值时不再往后看，避免「同一张卡片后来又发了一次」冒充本次。
    * get_messages 只支持 limit/offset、没有时间条件，所以分页往回翻：
      翻到「本页最老一条早于 since_ts」就停，避免活跃群消息多时把活动那条
      挤出最近一页、误判成没发到。
    """
    if detail is not None:
        detail.clear()
    if not conv_id:
        return "unreadable"
    hit = None
    for page in range(READ_MAX_PAGES):
        try:
            rows = db.get_messages(conv_id, limit=READ_LIMIT,
                                   offset=page * READ_LIMIT) or []
        except Exception:
            if hit is None:
                return "unreadable"
            break
        if not rows:
            break
        oldest = None            # 本页最早一条的时间（只看 ts>0 的）
        for r in rows:
            try:
                ts = int(r.get("create_time") or 0)
            except Exception:
                ts = 0
            if ts > 0 and (oldest is None or ts < oldest):
                oldest = ts
            if ts < since_ts:
                continue
            if until_ts and ts > until_ts:
                continue
            if me and (r.get("sender_username") or "") != me:
                continue
            txt = r.get("content") or ""
            if not match or match in txt:
                hit = r           # get_messages 按时间倒序：首条命中就是最新那条
                break
        if hit is not None:
            break
        if oldest is not None and oldest < since_ts:
            break                # 已翻到活动开始之前，再往前只会更早
    if hit is None:
        return "not-found"
    sid = _server_id_of(db, conv_id, hit)
    if detail is not None:
        detail.update({
            "create_time": hit.get("create_time"),
            "sender": hit.get("sender_username") or "",
            "local_id": hit.get("local_id"),
            "shard": hit.get("shard") or "",
            "server_id": sid,
        })
    if sid is None:
        return "unknown"
    return "delivered" if sid > 0 else "undelivered"


def _parse_result(raw):
    try:
        v = json.loads(raw or "[]")
    except Exception:
        return []
    if not isinstance(v, list):
        return []
    return [x for x in v if isinstance(x, dict)]


def main():
    ap = argparse.ArgumentParser(description="回刷转发活动结果")
    ap.add_argument("--apply", action="store_true", help="写回库（默认只统计）")
    ap.add_argument("--ids", default="", help="只处理指定活动 id，逗号分隔")
    ap.add_argument("--include-ok", action="store_true",
                    help="连非异常的条目也重新回读一遍（默认只动异常条目）")
    ap.add_argument("--debug", action="store_true",
                    help="打印每条命中消息的 server_id/时间，便于跨设备核对判据")
    args = ap.parse_args()

    want = set()
    for x in args.ids.replace("，", ",").split(","):
        x = x.strip()
        if x.isdigit():
            want.add(int(x))

    try:
        db = db_gate.get_db()
    except Exception as e:
        print("读不到微信解密库（微信没登录/没启动？）：%s" % e)
        print("回刷必须有微信库做证据，先让微信登录再跑。")
        return 2
    me = getattr(db, "wxid", "") or ""
    print("自身 wxid：%s（回读只认「我发出的」消息）"
          % (me or "未知——退化为不区分发送者"))

    conn = monitor_store.init()
    try:
        rows = conn.execute(
            "SELECT id,name,kind,status,send_at,targets,result,source_match "
            "FROM campaign ORDER BY id").fetchall()
    finally:
        conn.close()

    total_ok = total_fail = total_fix = 0
    touched_campaigns = 0

    for r in rows:
        cid = r["id"]
        if want and cid not in want:
            continue
        if (r["kind"] or "text") != "forward":
            continue
        results = _parse_result(r["result"])
        if not results:
            continue
        bad = [x for x in results if _is_bad(x.get("reason"))]
        if not bad and not args.include_ok:
            continue

        base_since = int(r["send_at"] or 0) - TS_TOLERANCE
        match = (r["source_match"] or "").strip()
        n_targets = len(_parse_result(r["targets"]) or [])
        print("\n=== 活动 %s 「%s」 status=%s 目标 %d 条 / 异常 %d 条 ==="
              % (cid, r["name"], r["status"], n_targets or len(results), len(bad)))
        if not match:
            print("  ! 该活动没有 source_match（卡片标题），只能按时间判定，准确度较低")

        new_results = []
        for x in results:
            need = _is_bad(x.get("reason")) or args.include_ok
            if not need:
                new_results.append(x)
                continue
            # 首选用「该条结果自己的记录时间 ts」定窗口：它就在这批实际发送的
            # 前后几分钟内，比活动计划时间 send_at 精确得多（活动可能排队很久
            # 才真正开始发）。窗口 = [ts-1h, ts+10min]，且不早于 send_at-容差。
            ets = int(x.get("ts") or 0)
            if ets:
                since = max(base_since, ets - LOOKBACK)
                until = ets + TS_TOLERANCE * 5
            else:
                since, until = base_since, 0
            detail = {}
            got = _landed(db, x.get("conv_id"), since, match,
                          until_ts=until, me=me, detail=detail)
            if args.debug:
                print("    证据: %s" % (detail or "窗口内无匹配的我发消息"))
            y = dict(x)
            if got == "delivered":
                y["ok"] = True
                y["reason"] = "已转发（回刷：我发出的消息已获服务器确认）"
            elif got == "undelivered":
                y["ok"] = False
                y["reason"] = ("未送达（回刷：本地有卡片但 server_id=0，"
                               "微信红色叹号/对方已删除或拒收）")
            elif got == "unknown":
                y["ok"] = False
                y["reason"] = "无法确认送达（回刷：消息在，但读不到服务器回执）"
            elif got == "unreadable":
                y["ok"] = False
                y["reason"] = "无法回读（回刷：微信库读不到该会话）"
            else:
                y["ok"] = False
                y["reason"] = ("已发出但回读未确认（回刷：活动后无对应卡片，"
                               "可能被删/未送达/已撤回）")
            y["ts"] = int(time.time())
            new_results.append(y)
            total_fix += 1
            print("  %-2s %-24s %s"
                  % ("✓" if y.get("ok") else "✗",
                     (y.get("name") or y.get("conv_id") or "")[:24],
                     y["reason"][:56]))

        nok = sum(1 for x in new_results if x.get("ok"))
        n = len(new_results)
        status = "sent" if n and nok == n else ("partial" if n else r["status"])
        print("  → 成功 %d / 未成功 %d，status %s → %s"
              % (nok, n - nok, r["status"], status))
        total_ok += nok
        total_fail += (n - nok)
        touched_campaigns += 1

        if args.apply:
            c = monitor_store.init()
            try:
                c.execute("UPDATE campaign SET result=?, status=?, updated=? WHERE id=?",
                          (json.dumps(new_results, ensure_ascii=False), status,
                           int(time.time()), cid))
                c.commit()
                print("  已写回库")
            finally:
                c.close()

    print("\n=== 合计：涉及活动 %d 个 / 回刷条目 %d / 成功 %d / 未成功 %d ==="
          % (touched_campaigns, total_fix, total_ok, total_fail))
    if not args.apply:
        print("（当前是预演，未改库。确认无误后加 --apply 写回）")


if __name__ == "__main__":
    main()
