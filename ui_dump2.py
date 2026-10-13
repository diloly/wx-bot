#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ui_dump2.py —— 读屏可行性验证 (UIA 无障碍树, 非截图/OCR)。

用法:
    1) pip install uiautomation        (若提示找不到模块)
    2) 把微信某个聊天窗口点成"前台焦点"
    3) python ui_dump2.py
    4) 看输出里是否出现聊天消息文字 (说话人/内容)

如果输出里能看到消息文本 -> UIA 在新版微信上走得通, 收信息可走这条路。
如果只有窗口框架、没有文字 -> 新版 webview 没暴露文字, 再换 xlog/CDP。
本脚本只读, 不改任何东西。
"""
import sys

try:
    import uiautomation as auto
except ImportError:
    print("[!] 需要 uiautomation: 运行  pip install uiautomation")
    sys.exit(1)


def main():
    auto.uiautomation.SetGlobalSearchTimeout(2)
    # 倒计时: 给用户时间把微信聊天窗口点成前台焦点
    # (否则 python 从 cmd 运行时, cmd 窗口会抢走前台, 抓到的是 cmd 而不是微信)
    for i in range(3, 0, -1):
        print(f"[*] {i} 秒后抓取前台窗口 —— 现在请用鼠标点一下微信的【聊天窗口】使其置顶 ...", flush=True)
        import time
        time.sleep(1)
    fg = auto.GetForegroundControl()
    if not fg:
        print("[!] 没拿到前台窗口, 请先把微信聊天窗口点成焦点")
        return

    title = fg.Name
    cls = fg.ClassName if hasattr(fg, "ClassName") else "?"
    print(f"[*] 前台窗口: title={title!r} class={cls!r}")
    print("[*] 开始 dump 无障碍树 (只打印有文字的节点, 最多 400 个)...")

    count = 0
    MAX = 400

    def walk(ctrl, depth):
        nonlocal count
        if count >= MAX:
            return
        try:
            name = ctrl.Name or ""
            ctype = ctrl.LocalizedControlType or ""
        except Exception:
            return
        if name.strip():
            pad = "  " * min(depth, 12)
            print(f"{pad}[{ctype}] {name}")
            count += 1
        try:
            children = ctrl.GetChildren()
        except Exception:
            children = []
        for ch in children:
            if count >= MAX:
                break
            walk(ch, depth + 1)

    walk(fg, 0)
    print(f"[*] 共打印 {count} 个含文字节点")


if __name__ == "__main__":
    main()
