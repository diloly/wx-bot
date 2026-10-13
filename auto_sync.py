#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
常驻自动同步：让看板「零手动」刷新。

每 N 秒（默认 60）自动：
  1. 调 PyWxDump 重新解密微信本地库（重新读密钥 + 解开当前 MSG.db）
  2. 调 ingest 把新消息导入统一消息库 wechat_monitor.db
看板 dashboard.py 已 2 秒轮询，所以新消息会自动显示，无需任何手动操作。

运行（和微信同一台 Windows，微信已登录并前台运行）：
    python auto_sync.py            # 常驻
    python auto_sync.py --once     # 只跑一轮，便于先验证 decrypt 能通

配置在 config.json 的 "sync" 块：
    enabled        : true/false
    wechat_dir     : 微信数据目录，如 "C:/Users/你/Documents/WeChat Files"
    out_dir        : 解密后 db 的输出目录（脚本会自动建）
    decrypt_cmd    : 解密命令模板，{wechat_dir} 和 {out_dir} 会被替换成上面的值。
                     新版 PyWxDump 的 CLI 入口是 `wxdump`（不再是 `python -m pywxdump`）。
                     解密通常需要先有密钥：可配 key_cmd 先用 `wxdump info` 从运行中的
                     微信提取并缓存密钥，decrypt 不传 -k 时会复用该缓存密钥。
                     不同 PyWxDump 版本参数不同，先 `wxdump decrypt -h` / `wxdump info -h`
                     看本机真实参数再填。
    interval_seconds: 同步间隔（秒）。越小越「实时」，但解密开销越大。

诚实边界：
  - 自动解密需要常驻读微信进程/密钥取 key，比纯键盘驱动多侵入一层，
    但仍是只读、不注入、不发消息，封号风险远低于 wcferry。
  - 你这 Qt 新版微信，PyWxDump 可能尚未适配；若 decrypt 报错，需等工具更新或回退微信旧版。
"""
import os
import sys
import time
import json
import logging
import subprocess

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("auto_sync")

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.json")


def load_sync_cfg():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    sc = cfg.get("sync")
    if not sc:
        raise SystemExit("config.json 缺少 'sync' 配置块，请参考下面的示例补上。")
    for k in ("wechat_dir", "out_dir", "decrypt_cmd"):
        if not sc.get(k):
            raise SystemExit(f"config.json 的 sync.{k} 不能为空")
    sc.setdefault("interval_seconds", 60)
    sc.setdefault("key_cmd", "")
    return sc


def _run_cmd_template(sc, key):
    """把 config 里的命令模板（含 {wechat_dir}/{out_dir}）展开后执行。"""
    wechat_dir = sc["wechat_dir"]
    out_dir = sc["out_dir"]
    cmd = (sc[key]
           .replace("{wechat_dir}", wechat_dir)
           .replace("{out_dir}", out_dir))
    log.info("执行: %s", cmd)
    import shlex
    try:
        proc = subprocess.run(
            shlex.split(cmd), capture_output=True, text=True, timeout=180
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{key} 超时（180s），可能卡在交互输入或工具不兼容。")
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-800:]
        raise RuntimeError(f"{key} 返回非零（{proc.returncode}）：\n{tail}")
    return out_dir


def run_decrypt(sc):
    """执行解密命令。成功返回 out_dir；失败抛错。

    若配置了 key_cmd（先用 wxdump info 从运行中的微信提取密钥），会先跑它再解密。
    """
    os.makedirs(sc["out_dir"], exist_ok=True)
    if sc.get("key_cmd"):
        try:
            _run_cmd_template(sc, "key_cmd")
        except Exception as e:  # noqa: BLE001
            log.warning("key_cmd 失败（部分版本 decrypt 可复用缓存密钥，可忽略）: %s", e)
    return _run_cmd_template(sc, "decrypt_cmd")


def sync_once(sc):
    from ingest import ingest
    out_dir = run_decrypt(sc)
    before = _count_messages()
    ingest(out_dir)
    after = _count_messages()
    log.info("本轮导入后消息总数：%d（新增约 %d）", after, after - before)


def _count_messages():
    try:
        from monitor_store import _conn
        conn = _conn()
        n = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        conn.close()
        return n
    except Exception:
        return 0


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="只跑一轮同步后退出（便于验证 decrypt 能否跑通）")
    args = ap.parse_args()

    sc = load_sync_cfg()
    if not sc.get("enabled", True) and not args.once:
        log.info("sync.enabled=false，未启动常驻。需要同步请改 config 或加 --once。")
        return

    if args.once:
        try:
            sync_once(sc)
            log.info("--once 完成。若上方有报错，先解决 decrypt 再常驻。")
        except Exception as e:  # noqa: BLE001
            log.error("本轮失败: %s", e)
        return

    log.info("常驻自动同步启动，间隔 %ds。Ctrl+C 退出。", sc["interval_seconds"])
    try:
        while True:
            try:
                sync_once(sc)
            except Exception as e:  # noqa: BLE001
                log.error("本轮失败（下轮重试）: %s", e)
            time.sleep(sc["interval_seconds"])
    except KeyboardInterrupt:
        log.info("已停止。")


if __name__ == "__main__":
    main()
