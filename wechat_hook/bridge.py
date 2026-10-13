#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bridge.py —— Python 侧: 连接 hookdll 创建的命名管道, 读取回传数据。

不依赖 pywin32, 用 ctypes 直接调 kernel32。
用法:
    python bridge.py
然后另开一个终端用 injector 把 hookdll.dll 注入微信 (Weixin.exe)。

收到 [MSG] 行后, 解析出发送者(wxid/群id)与消息正文,
写入 monitor_store (wechat_monitor.db, role='them'), 看板 (dashboard.py) 自动刷新。
[export] 行是 DLL 自诊输出 (列出微信进程里导出的 sqlite3 符号), 落 recv_raw.log 备查。
"""
import os
import sys
import time
import re
import ctypes
from ctypes import wintypes

# 让本脚本能 import 到项目根目录的 monitor_store
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from monitor_store import add_message  # noqa: E402

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateFileW.restype = wintypes.HANDLE
kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                 ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
kernel32.ReadFile.restype = wintypes.BOOL
kernel32.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                              ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]

PIPE = r"\\.\pipe\wechat_hook_pipe"
GENERIC_RW = 0xC0000000
OPEN_EXISTING = 3
INVALID_HANDLE = wintypes.HANDLE(-1).value

RAW_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recv_raw.log")

# 发送者标识特征: wxid_xxx / 群 xxx@xxx / 纯数字 roomid
CONV_RE = re.compile(r"(wxid_[A-Za-z0-9_-]+|[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+|\d{6,})")


def log_raw(line: str):
    try:
        with open(RAW_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def guess_conv(fields):
    for f in fields:
        m = CONV_RE.search(f)
        if m:
            return m.group(1)
    return "wechat_recv"


def handle_msg(payload: str):
    """[MSG] SQL=... BINDS=b0||b1||..."""
    if " BINDS=" in payload:
        _sql, binds = payload.split(" BINDS=", 1)
    else:
        _sql, binds = payload, ""
    fields = [b for b in binds.split(" || ") if b]
    conv = guess_conv(fields)
    body = max(fields, key=len) if fields else "(无文本字段)"
    try:
        add_message(conv, "them", body, raw=payload)
        print(f"[store] + [{conv}] {body[:60]}")
    except Exception as e:  # noqa: BLE001
        print("[-] 写库失败:", e)


def main():
    print(f"等待连接管道 {PIPE} ...\n(先开微信 -> 再用 injector.exe 注入 hookdll.dll)")
    buf = b""
    while True:
        h = open_pipe()
        if not h:
            time.sleep(2)
            continue
        print("[+] 已连接 DLL")
        try:
            while True:
                chunk = ctypes.create_string_buffer(4096)
                n = wintypes.DWORD(0)
                ok = kernel32.ReadFile(h, chunk, 4096, ctypes.byref(n), None)
                if not ok or n.value == 0:
                    break
                buf += chunk[:n.value]
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    text = line.decode("utf-8", "ignore").strip()
                    if not text:
                        continue
                    print("[wechat]", text)
                    log_raw(text)
                    if text.startswith("[MSG] "):
                        handle_msg(text[len("[MSG] "):])
        except Exception as e:  # noqa: BLE001
            print("[-] 异常:", e)
        finally:
            kernel32.CloseHandle(h)
            print("[-] 管道断开, 重连中...")


def open_pipe():
    h = kernel32.CreateFileW(PIPE, GENERIC_RW, 0, None, OPEN_EXISTING, 0, None)
    if h == INVALID_HANDLE:
        return None
    return h


if __name__ == "__main__":
    main()
