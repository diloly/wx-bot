#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
export_history.py —— 聊天记录导出备份（wechatauto-replica 的 export_history 封装）
==================================================================================
把微信聊天记录导出成 JSON，用于：
  1. 定期备份（wechat_monitor.db 之外的原始底档，含媒体消息结构信息）；
  2. 客户资料沉淀（交接/复盘时按会话翻）。

⚠️ 导出文件含**客户聊天内容**，属于敏感数据：
  - 输出目录 backups/ 已在 .gitignore，不会进 git；
  - 不要外发、不要截图、不要传到公网。

用法：
    python export_history.py                          # 全量导出到 backups/history_时间戳.json
    python export_history.py --user wxid_xxx           # 只导指定会话（--user 可重复多次）
    python export_history.py --limit 500               # 每会话最多导 500 条（默认全部）
    python export_history.py --out backups/mydata.json   # 自定义输出路径

依赖：wechatauto-replica（装在跑得通 auto_reply.py 的那个 Python 里）。
"""
import argparse
import os
import sys
import time

try:
    from wechatauto import WeChatDB
except ImportError:
    print("[!] 需要 wechatauto-replica: pip install wechatauto-replica")
    sys.exit(1)
import db_gate

# 备份目录：与脚本同目录的 backups/（.gitignore 已覆盖，不会误提交客户数据）
BACKUP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backups")


def main():
    ap = argparse.ArgumentParser(description="导出微信聊天记录为 JSON 备份")
    ap.add_argument("--user", action="append", default=None,
                    help="只导出指定会话（wxid 或群 id），可重复多次；不给=全部会话")
    ap.add_argument("--limit", type=int, default=None,
                    help="每会话导出的条数上限（默认不限）")
    ap.add_argument("--out", default=None,
                    help="输出文件路径（默认 backups/history_YYYYmmdd_HHMMSS.json）")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    os.makedirs(BACKUP_DIR, exist_ok=True)
    out = args.out or os.path.join(
        BACKUP_DIR, "history_%s.json" % time.strftime("%Y%m%d_%H%M%S"))

    print("[*] 连接微信数据库（首轮要全量解密，可能数分钟）...")
    try:
        db = db_gate.get_db()
        me = db.get_self_info()
        print("[*] 当前账号: %s (%s)" % (me.get("nick_name"), me.get("username")))
    except Exception as e:
        print("[!] WeChatDB 初始化失败（微信需已登录并跑过 wechatauto 体检）: %s" % e)
        sys.exit(1)

    users = args.user or None
    scope = "%d 个指定会话" % len(users) if users else "全部会话"
    print("[*] 导出范围: %s%s" % (scope, "，每会话上限 %d 条" % args.limit if args.limit else ""))
    print("[*] 输出: %s" % out)

    last_pct = [-1]

    def progress(done, total, name):
        if total <= 0:
            return
        pct = int(done * 100 / total)
        if pct >= last_pct[0] + 10 or done == total:
            last_pct[0] = pct
            print("[export] %d/%d (%d%%) %s" % (done, total, pct, name))

    t0 = time.time()
    try:
        stat = db.export_history(
            out, fmt="json", users=users, limit_per_chat=args.limit,
            progress=progress)
    except Exception as e:
        print("[!] 导出失败: %s" % e)
        sys.exit(1)

    size = os.path.getsize(out) if os.path.isfile(out) else 0
    print("[OK] 导出完成：%s（%.1f KB，耗时 %.1fs）"
          % (out, size / 1024.0, time.time() - t0))
    if isinstance(stat, dict):
        print("[OK] 统计: %s" % stat)
    print("[!] 该文件含客户聊天内容，属敏感数据：不要外发/截图/传公网；"
          "backups/ 已在 .gitignore，不会进 git。")


if __name__ == "__main__":
    main()
