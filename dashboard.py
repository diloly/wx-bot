#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
微信客户维护 —— 网页看板（仿微信：左会话列表 + 右会话详情）。
数据来自统一消息库 wechat_monitor.db（ingest 导入收件 + wechat_ui 写入发件）。

运行（在你本机，和微信同一台 Windows）：
    pip install -r requirements.txt
    python dashboard.py
然后浏览器打开 http://localhost:5000

API：
    GET  /api/conversations       会话列表
    GET  /api/messages?conv=<id>  某会话消息
    POST /api/open  {conv}        标记已读（清未读）
    POST /api/send  {conv,text}   触发键盘发送（需微信在前台；会写回发送记录）
"""
import os
import logging

from flask import Flask, render_template, jsonify, request

from monitor_store import (
    init, list_conversations, get_messages, add_message, clear_unread,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("dashboard")

init()
BASE = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, template_folder=os.path.join(BASE, "templates"))


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/conversations")
def api_conversations():
    return jsonify(list_conversations())


@app.route("/api/messages")
def api_messages():
    conv = request.args.get("conv", "")
    return jsonify(get_messages(conv))


@app.route("/api/open", methods=["POST"])
def api_open():
    data = request.get_json(force=True, silent=True) or {}
    conv = data.get("conv", "")
    if conv:
        clear_unread(conv)
    return jsonify({"ok": True})


@app.route("/api/send", methods=["POST"])
def api_send():
    """通过键盘驱动把消息发给指定联系人（需微信在前台）。"""
    data = request.get_json(force=True, silent=True) or {}
    conv, text = data.get("conv", "").strip(), data.get("text", "").strip()
    if not conv or not text:
        return jsonify({"ok": False, "error": "conv 和 text 不能为空"}), 400
    try:
        import wechat_ui
        cfg = wechat_ui.load_config()
        cfg["dry_run"] = False
        bot = wechat_ui.WeChatPCBot(cfg)
        bot.send_to_contact(conv, text)   # 内部按键盘流程真发，并写回 store
        return jsonify({"ok": True, "conv": conv})
    except Exception as e:  # noqa: BLE001
        log.exception("发送失败")
        return jsonify({"ok": False, "error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("WECHAT_DASHBOARD_PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
