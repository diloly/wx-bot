#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ui_dump3.py (v2) —— 枚举所有可见顶层窗口并打印完整清单, 再 dump 微信相关窗口的 a11y 树。
v1 的 bug: 用 OpenProcess 逐个窗口取进程名, 取不到就回退成 "pid=xxx" 然后被过滤条件
静默丢弃, 导致 WeChatAppEx.exe 窗口没出现。
v2 改用 CreateToolhelp32Snapshot 一次性拿 pid->exe 名 (不依赖 OpenProcess), 且先打印
全部窗口清单, 一眼能看出有没有 WeChatAppEx / Chrome 类窗口。纯只读。

用法:
    pip install uiautomation
    python ui_dump3.py
"""
import sys
import ctypes
from ctypes import wintypes

try:
    import uiautomation as auto
except ImportError:
    print("[!] 需要 uiautomation: 运行  pip install uiautomation")
    sys.exit(1)

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)


class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.CHAR * 260),
    ]


def enum_processes():
    """返回 {pid: exe_name}。用快照, 不依赖 OpenProcess 权限。"""
    result = {}
    h = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    if h == wintypes.HANDLE(-1).value:
        return result
    pe = PROCESSENTRY32()
    pe.dwSize = ctypes.sizeof(pe)
    if kernel32.Process32First(h, ctypes.byref(pe)):
        while True:
            name = pe.szExeFile.decode("utf-8", "ignore")
            result[pe.th32ProcessID] = name
            if not kernel32.Process32Next(h, ctypes.byref(pe)):
                break
    kernel32.CloseHandle(h)
    return result


def main():
    auto.uiautomation.SetGlobalSearchTimeout(1)
    procs = enum_processes()
    target_pids = set()
    for pid, name in procs.items():
        nl = name.lower()
        if "weixin" in nl or "wechat" in nl:
            target_pids.add(pid)

    print("[*] 枚举所有可见顶层窗口 ...")
    HWNDS = []

    def enum_proc(hwnd, lparam):
        if user32.IsWindowVisible(hwnd):
            HWNDS.append(hwnd)
        return True

    user32.EnumWindows(EnumWindowsProc(enum_proc), 0)
    print("[*] 共 %d 个可见顶层窗口, 完整清单如下:" % len(HWNDS))
    print("%-10s %-22s %-30s %s" % ("HWND", "PROCESS", "CLASS", "TITLE"))
    print("-" * 100)

    wechat_windows = []
    for hwnd in HWNDS:
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        pname = procs.get(pid.value, "pid=%d(未知)" % pid.value)
        length = user32.GetWindowTextLengthW(hwnd)
        title = ""
        if length:
            b = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, b, length + 1)
            title = b.value
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        print("%-10s %-22s %-30s %s" % (str(hwnd), pname[:22], cls.value[:30], title[:40]))
        if pid.value in target_pids:
            wechat_windows.append((hwnd, pid.value, pname, title, cls.value))

    print("\n[*] 微信相关窗口 (pid 命中 Weixin/WeChatAppEx): %d 个" % len(wechat_windows))
    for hwnd, pid, pname, title, cls in wechat_windows:
        print("  HWND=%s pid=%d proc=%s title=%r class=%r" % (hwnd, pid, pname, title, cls))

    for hwnd, pid, pname, title, cls in wechat_windows:
        print("\n===== dump HWND=%s proc=%s title=%r class=%r =====" % (hwnd, pname, title, cls))
        try:
            ctrl = auto.ControlFromHandle(hwnd)
        except Exception as e:
            print("[!] ControlFromHandle 失败: %s" % e)
            continue
        if ctrl is None:
            print("[!] 该 HWND 不是有效 UIA 元素")
            continue
        count = [0]
        MAX = 300

        def walk(c, depth):
            if count[0] >= MAX:
                return
            try:
                name = c.Name or ""
                ctype = c.LocalizedControlType or c.ControlTypeName or ""
            except Exception:
                return
            if name.strip():
                print("%s[%s] %s" % ("  " * min(depth, 12), ctype, name))
                count[0] += 1
            try:
                ch = c.GetChildren()
            except Exception:
                ch = []
            for x in ch:
                if count[0] >= MAX:
                    break
                walk(x, depth + 1)

        walk(ctrl, 0)
        print("[*] 该窗口含文字节点: %d" % count[0])

    print("\n[*] 完成。")
    print("    如果上面完整清单里压根没有 WeChatAppEx.exe / Chrome 类窗口 -> webview 内容被嵌进 Qt 主窗口, UIA 整体判死。")
    print("    如果某窗口出现了聊天消息文字, 把那段贴给我。")


if __name__ == "__main__":
    main()
