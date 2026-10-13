#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
诊断：列出当前标题含「微信」的窗口（ctypes 实现，不依赖 UIA / 控件树）。
用于确认键盘驱动能否定位到个人微信窗口，并确认企业微信已被排除。
用法：python diag_windows.py
"""
import ctypes
from ctypes import wintypes

_user32 = ctypes.windll.user32


def main():
    all_wins = []

    def _enum(hwnd, _lparam):
        if not _user32.IsWindowVisible(hwnd):
            return
        length = _user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return
        buf = ctypes.create_unicode_buffer(length + 1)
        _user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value
        if "微信" in title:
            all_wins.append((hwnd, title))

    ENUMPROC = ctypes.WINFUNCTYPE(None, wintypes.HWND, wintypes.LPARAM)
    _user32.EnumWindows(ENUMPROC(_enum), 0)

    if not all_wins:
        print("（未找到标题含「微信」的窗口：说明微信没打开/没登录）")
        return

    print(f"共发现 {len(all_wins)} 个标题含「微信」的窗口：")
    selected = None
    for hwnd, title in all_wins:
        is_wecom = ("企业" in title) or ("开发者" in title)
        if is_wecom:
            tag = "企业微信/其他（已排除，不会被脚本识别）"
        else:
            tag = "✅ 个人微信（会被脚本选中）"
            if selected is None:
                selected = (hwnd, title)
        print(f"  hwnd={hwnd}  title={title!r}  -> {tag}")

    if selected:
        print(f"\n脚本将使用：hwnd={selected[0]} title={selected[1]!r}")
    else:
        print("\n⚠️ 未发现个人微信窗口（只有企业微信等）。请打开个人微信。")
        print("   注：本脚本故意不识别企业微信，避免误操作系统窗口。")


if __name__ == "__main__":
    main()
