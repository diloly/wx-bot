# -*- coding: utf-8 -*-
"""
ui_dump4.py —— 微信(新版 Weixin) 控件树深度探查
目的: 验证 UIA(Windows UI Automation) 在这台机器上到底能不能拿到聊天消息文字。
       之前 ui_dump2/3 只 dump 了壳窗口的浅层子树(根下只有 MMUIRenderSubWindowHW 占位节点)，
       就草率下了「UIA 走不通」的结论。本脚本做真正无限的整树递归 + 鼠标定位两种模式。

用法:
  1) 全树递归模式(默认):
       python ui_dump4.py
     -> 3 秒倒计时, 期间把微信某个聊天窗口点成前台焦点。
        脚本从「前台窗口」开始, 递归遍历整个 UIA 子树(不限深度),
        打印所有有 Name 或 ClassName 的节点(带层级缩进)。

  2) 鼠标定位模式(精准验证某条消息):
       python ui_dump4.py --pick
     -> 把鼠标移到某条聊天消息文字上, 按回车,
        脚本打印「鼠标处控件」从根到目标的完整祖先路径 + 各层属性。
        如果路径末端出现消息文本 -> UIA 通; 如果只到渲染层 -> 真死。

依赖:
     pip install uiautomation
纯只读, 不注入/不改微信。
"""
import sys
import time
import uiautomation as auto

MAX_DEPTH = 80       # 递归最大深度(微信 4.x 控件树可能很深)
MAX_NODES = 8000     # 最多打印的节点数(防爆炸)

# 这几个类型即使没有 Name 也值得打印(往往是消息/列表容器)
INTERESTING_TYPES = ("文本", "编辑", "列表", "列表项", "文档", "树", "树项目", "按钮", "超链接")


def collect(control, depth, counter, out_lines):
    if counter[0] >= MAX_NODES or depth > MAX_DEPTH:
        return
    try:
        name = control.Name or ""
        ctype = control.LocalizedControlType or ""
        cls = control.ClassName or ""
    except Exception:
        return
    counter[0] += 1

    interesting = bool(name) or bool(cls) or (ctype in INTERESTING_TYPES)
    if interesting:
        indent = "  " * min(depth, MAX_DEPTH)
        line = "{}[{}] name={!r} class={!r}".format(indent, ctype, name, cls)
        print(line)
        out_lines.append(line)

    try:
        children = control.GetChildren()
    except Exception:
        children = []
    for ch in children:
        collect(ch, depth + 1, counter, out_lines)


def dump_foreground():
    auto.SetGlobalSearchTimeout(2)
    print("[*] 3 秒后深度遍历前台窗口, 请把鼠标点进微信聊天窗口使其置顶...")
    time.sleep(3)
    fg = auto.GetForegroundControl()
    try:
        fname = fg.Name
        fcls = fg.ClassName
    except Exception:
        fname, fcls = "", ""
    print("[*] 前台窗口: name={!r} class={!r}".format(fname, fcls))
    print("[*] 开始递归整棵控件树 (最多 {} 节点 / 深度 {})...".format(MAX_NODES, MAX_DEPTH))

    out = []
    counter = [0]
    collect(fg, 0, counter, out)

    print("[*] 共打印 {} 个节点".format(counter[0]))
    if counter[0] <= 5:
        print("[!] 节点极少 -> 该窗口未向 UIA 暴露任何子控件 (webview/自绘封闭), UIA 确实走不通")
    else:
        # 粗略判断是否有疑似消息文字
        texty = [l for l in out if "文本" in l and len(l) > 20]
        print("[*] 含较长文本型节点 {} 个 (若有聊天文字应在此列)".format(len(texty)))


def pick_mode():
    auto.SetGlobalSearchTimeout(2)
    input("[*] 鼠标定位模式: 把鼠标移到一条聊天消息文字上, 然后按回车...")
    try:
        x, y = auto.GetCursorPos()
        c = auto.ControlFromPoint(x, y)
    except Exception as e:
        print("[!] 取鼠标处控件失败: {}".format(e))
        return

    chain = []
    cur = c
    while cur is not None:
        try:
            chain.append("[{}] name={!r} class={!r}".format(
                cur.LocalizedControlType or "", cur.Name or "", cur.ClassName or ""))
        except Exception:
            pass
        try:
            cur = cur.GetParentControl()
        except Exception:
            break

    print("[*] 鼠标处控件路径 (从根到目标, 每层缩进):")
    for i, n in enumerate(reversed(chain)):
        print("  " * i + n)


def main():
    if "--pick" in sys.argv:
        pick_mode()
    else:
        dump_foreground()


if __name__ == "__main__":
    main()
