#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wxauto_test.py —— 验证 wxauto4 能否在你的微信(Weixin)上「读 + 发」消息。

机制：Windows UI Automation（读前台聊天窗口的无障碍树 + 模拟点击发送）。
非注入、非 hook、非协议破解，符合「非侵入」偏好（和你之前要的方向一致）。

前置：
  1. 微信已登录且前台运行。
  2. pip install wxauto4   （免费版，支持 WeChat 4.1.x；仅支持 py3.9~3.12）
     若你是 py3.13，免费版装不上，需用付费 Plus：pip install wxautox4（支持 3.13）

运行：
  python wxauto_test.py
"""
from wxauto4 import WeChat


def main():
    wx = WeChat()
    print("[+] wxauto 已连接到微信")

    target = "文件传输助手"
    wx.ChatWith(target)
    info = wx.ChatInfo()
    print("[+] 当前窗口信息:", info)

    # 读取当前聊天窗口的历史消息
    msgs = wx.GetAllMessage()
    print("[+] 读取到 %d 条消息:" % len(msgs))
    for m in msgs:
        print("    ", getattr(m, "raw", m))

    # 发一条测试消息（发到文件传输助手，不会打扰真实联系人）
    wx.SendMsg("你好（wxauto 自测）")
    print("[+] 已发送测试消息到:", target)


if __name__ == "__main__":
    main()
