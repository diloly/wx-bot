#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ui_gate.py —— 微信 UI 独占门（跨进程 + 进程内，排队不丢弃）
=================================================================================
为什么需要它：**微信 PC 只有一份 UI**（一个主窗口、一份键鼠焦点）。所有会
占用前台的动作——键盘发送、朋友圈刷新、点赞/评论、页面导航——必须串行，
否则两边互相抢前台，历史上出过两类事故：
  ① Ctrl+F/粘贴/Enter 发进了看板浏览器（2026-10-15 窗口误判事故）；
  ② 朋友圈刷新到一半被消息发送打断切回聊天页，find_moment 在聊天页白跑，
     库打「滚轮落点不属于微信 (0,0)」「未能定位到目标朋友圈」（2026-10-16）。

两条硬约束（2026-10-16 用户拍板）：
  - **操作不能丢弃**：拿不到门就排队等，绝不是 try-lock 失败就静默跳过。
  - **只能排队**：同一时刻全机只允许一个操作持门。

实现分层（两层都要，缺一不可）：
  * 跨进程：Windows 命名 Mutex（CreateMutexW + WaitForSingleObject）。
    auto_reply.py 与 dashboard.py 是**两个进程**，各自都会占 UI——只做线程锁
    挡不住。选 Mutex 而不是锁文件，是因为它有内核级「持有者退出即释放」：
    持门进程崩溃/被 Ctrl+C 时，等待者的 WaitForSingleObject 返回
    WAIT_ABANDONED 并**直接获得所有权**，天然自愈，不会留下永远解不开的
    死锁（锁文件方案必须自己写租约+过期，容易出错）。
  * 进程内：threading.RLock。dashboard 是 Flask(threaded=True)，同进程多请求
    会并发（看板两个按钮同时点）；RLock 顺带支持**同线程重入**——「一段业务
    里再调底层动作」不会自己把自己锁死（例：给 auto_interact 整段包门时，
    内部逐条调的 like() 不会再阻塞）。

等待与超时：
  - 默认单次等待上限 180s（config.json 的 ui.lock_timeout_seconds 可改）。
    单个 UI 动作最长约 15s（朋友圈刷新含 4s 同步等待），队形正常时等几秒
    就到；180s 够覆盖十几个积压操作，再多说明 UI 真卡死了。
  - 超时抛 UITimeoutError，由调用方决定怎么记/怎么报——**不静默吞掉**。
    手动操作（看板按钮）会把它变成明确报错，让用户重试；自动操作记日志、
    放弃这一条（不阻塞主循环），下一轮轮询照常。
  - 等超过 WARN_AFTER 秒会打印一行「等了多久才拿到」，方便排查谁在占 UI。

用法：
    import ui_gate
    with ui_gate.hold("发送给 黄文业"):
        ...键盘流程/朋友圈点击...
"""
import contextlib
import json
import os
import sys
import threading
import time

DEFAULT_TIMEOUT = 180.0      # 单次等待上限（秒）
WARN_AFTER = 2.0             # 等待超过该秒数就打印一条等待日志
MUTEX_NAME = "WechatBotUI"   # 跨进程命名互斥体（同一登录会话内互通）
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "config.json")


class UITimeoutError(RuntimeError):
    """等待 UI 门超时：微信被别的操作长时间占用（或界面卡死）。"""


# ---------------------------------------------------------------- 跨进程互斥
class _NamedMutex:
    """Windows 命名 Mutex 封装（拿不到就等，绝不失败即弃）。"""

    def __init__(self, name):
        import ctypes
        from ctypes import wintypes
        # 必须显式声明 restype/argtypes：HANDLE 在 64 位下是 8 字节，
        # ctypes 默认按 c_int 收会截断句柄（经典坑）。
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = wintypes.HANDLE
        k32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL,
                                     wintypes.LPCWSTR]
        k32.WaitForSingleObject.restype = wintypes.DWORD
        k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k32.ReleaseMutex.restype = wintypes.BOOL
        k32.ReleaseMutex.argtypes = [wintypes.HANDLE]
        self._k32 = k32
        self._name = name
        self._handle = k32.CreateMutexW(None, False, name)
        if not self._handle:
            raise OSError("CreateMutexW 失败(%s): err=%s"
                          % (name, ctypes.get_last_error()))

    def acquire(self, timeout):
        WAIT_OBJECT_0 = 0x0
        WAIT_ABANDONED = 0x80
        WAIT_TIMEOUT = 0x102
        ms = 0xFFFFFFFF if timeout is None else int(max(0.0, timeout) * 1000)
        r = self._k32.WaitForSingleObject(self._handle, ms)
        if r == WAIT_OBJECT_0:
            return "ok"
        if r == WAIT_ABANDONED:
            return "abandoned"          # 上一个持有者进程已退出，我们接管
        if r == WAIT_TIMEOUT:
            raise UITimeoutError(
                "等待微信 UI 空闲超过 %.0fs（有别的进程/操作一直占着）" % timeout)
        raise OSError("WaitForSingleObject 返回 %s" % r)

    def release(self):
        if not self._k32.ReleaseMutex(self._handle):
            import ctypes
            print("[ui] ReleaseMutex 失败（非持有线程？）: err=%s"
                  % ctypes.get_last_error())


class _NullMutex:
    """非 Windows（或 Mutex 创建失败）时的退化实现：只靠进程内锁。"""

    def acquire(self, timeout):
        return "ok"

    def release(self):
        pass


# ---------------------------------------------------------------- 门本体
class UIGate:
    """全机唯一 UI 资源的门。模块级单例，业务侧统一用 ui_gate.hold()。"""

    def __init__(self, name=MUTEX_NAME, timeout=DEFAULT_TIMEOUT):
        self._name = name
        self._timeout = float(timeout)
        self._rlock = threading.RLock()   # 进程内排队 + 同线程重入
        self._depth = {}                  # tid -> 重入深度（只在持有 _rlock 时读写）
        if sys.platform == "win32":
            try:
                self._mutex = _NamedMutex(name)
            except Exception as e:        # noqa: BLE001
                print("[ui] 跨进程 UI 锁不可用，退化为仅进程内互斥: %s" % e)
                self._mutex = _NullMutex()
        else:
            self._mutex = _NullMutex()

    def acquire(self, what="", timeout=None):
        """排队拿门。返回实际等待秒数。超时抛 UITimeoutError。"""
        timeout = self._timeout if timeout is None else float(timeout)
        t0 = time.time()
        self._rlock.acquire()             # 进程内排队（同线程直接重入成功）
        tid = threading.get_ident()
        if self._depth.get(tid, 0) == 0:  # 本线程是进程内第一个进门的
            try:
                tag = self._mutex.acquire(timeout)
            except UITimeoutError:
                self._rlock.release()
                raise
            except Exception as e:        # noqa: BLE001
                self._rlock.release()
                raise UITimeoutError("获取跨进程 UI 锁失败: %s" % e)
            if tag == "abandoned":
                print("[ui] 检测到上一个进程异常退出留下的锁，已自动接管")
            waited = time.time() - t0
            if waited >= WARN_AFTER:
                print("[ui] %s：排队 %.1fs 后取得 UI（有别的操作在占微信）"
                      % (what or "操作", waited))
        self._depth[tid] = self._depth.get(tid, 0) + 1
        return time.time() - t0

    def release(self):
        tid = threading.get_ident()
        d = self._depth.get(tid, 0)
        if d <= 1:
            self._depth.pop(tid, None)
            try:
                self._mutex.release()
            except Exception as e:        # noqa: BLE001
                print("[ui] 释放 UI 锁异常: %s" % e)
        else:
            self._depth[tid] = d - 1
        self._rlock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()
        return False

    @contextlib.contextmanager
    def hold(self, what="", timeout=None):
        self.acquire(what, timeout)
        try:
            yield self
        finally:
            self.release()


# ---------------------------------------------------------------- 模块级单例
_gate = None
_gate_lock = threading.Lock()


def _load_timeout():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        return float((cfg.get("ui") or {}).get("lock_timeout_seconds",
                                               DEFAULT_TIMEOUT))
    except Exception:                     # noqa: BLE001
        return DEFAULT_TIMEOUT


def get_gate():
    global _gate
    if _gate is None:
        with _gate_lock:
            if _gate is None:
                _gate = UIGate(timeout=_load_timeout())
    return _gate


def hold(what="", timeout=None):
    """上下文管理器：独占微信 UI。用法 with ui_gate.hold("发送给 XX"): ..."""
    return get_gate().hold(what, timeout)
