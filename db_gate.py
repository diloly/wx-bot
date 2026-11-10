#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""db_gate.py —— WeChatDB 解密缓存隔离门（一进程一份副本、一个实例）
================================================================================
事故背景（2026-11-10，`python auto_reply.py` 启动报 WinError 5）::

    [!] WeChatDB 初始化失败: [WinError 5] 拒绝访问。
    '...\\wechatauto_db\\wxid_xxx\\contact__contact.db.20576.db58.tmp'
    -> '...\\wechatauto_db\\wxid_xxx\\contact__contact.db'

根因有两层，都在 wechatauto 的解密缓存机制里：

1. **全进程共享同一份缓存目录**。``_open_unlocked()`` 把解密后的库写到
   ``%TEMP%/wechatauto_db/<账号>/``，主库或 WAL 一有变化就 ``os.replace(tmp, dst)``
   重建副本；随后用 ``sqlite3.connect(file:dst?mode=ro)`` 打开只读连接。
   Windows 下该文件只要被**另一个进程**的 SQLite 连接持有，覆盖就是
   ``[WinError 5] 拒绝访问``。dashboard.py 的后台头像/通讯录同步线程常驻并持有
   WeChatDB，于是后启动的 auto_reply.py 必然撞上。目录里成堆的
   ``.tmp / -shm / -wal`` 残留就是两边反复互踩留下的。
2. **构建锁是实例级的**。``_build_lock_for()`` 用的是 ``self._build_locks``，
   每个 WeChatDB 实例各有一把。auto_reply.py 原本建了 3 个实例
   （主线程 self.db / AI 线程 _ai_db_obj / 发送线程 _send_db_obj），
   同进程内也会并发解密同一个 rel、并发 replace 同一个 dst，彼此踩中间产物。

修复口径（不改动第三方库，只约束项目自己的创建方式）：

* **一进程一份私有副本**：构造后把 ``db.workdir`` / ``db.keys_file`` 改到
  ``%TEMP%/wechatauto_db/<账号>/proc_<pid>/``。副本、stamp、tmp、shm、wal
  全部只属于本进程，别的进程永远不会 replace 我正在读的文件。
  代价：每个进程首次要自己解密一遍（contact ~260KB / message ~3MB / sns ~2MB，
  秒级），磁盘多占几 MB —— 相比启动直接失败，完全可接受。
* **一进程一个实例**：模块级单例 + RLock。所有线程（收消息 / AI / 发送校验 /
  朋友圈 / 看板头像同步）共用同一个 WeChatDB 对象。库内部每次查询都是
  ``_open(rel)`` 新开连接、用完 close，且同一 rel 的解密由实例锁串行；
  共享对象反而让构建锁统一，比多实例更安全。
* **跨进程命名 Mutex** 只保护**构造这一小段**：``_load_or_extract_keys()``
  可能写共享的 ``keys.json.tmp``（固定名），多进程同时写会撞。构造完就放锁，
  不长期占用。
* **WinError 5 自愈**：预热 ``get_self_info()`` 打开 contact 库，若仍被占用，
  清掉本进程私有目录里的中间产物后重试（最多 2 次）。stamp 没写成功，
  重试会重新走一遍构建，不会用到半截副本。

用法::

    import db_gate
    db = db_gate.get_db()      # 所有入口统一用这个，不要再直接 WeChatDB()
"""
import contextlib
import glob
import os
import shutil
import sys
import tempfile
import threading
import time

MUTEX_NAME = "WechatBotDBInit"   # 与 ui_gate 的 WechatBotUI 分开，互不阻塞
DEFAULT_TIMEOUT = 90.0           # 构造阶段的排队上限（秒）
SUBDIR_PREFIX = "proc_"


class DBInitError(RuntimeError):
    """等待微信数据库构造锁超时（有别的进程长时间卡在密钥提取）。"""


# ---------------------------------------------------------------- 跨进程互斥
class _NamedMutex:
    """Windows 命名 Mutex：持有者退出即释放，崩溃不会留下死锁。"""

    def __init__(self, name):
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = wintypes.HANDLE
        k32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL,
                                     wintypes.LPCWSTR]
        k32.WaitForSingleObject.restype = wintypes.DWORD
        k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k32.ReleaseMutex.restype = wintypes.BOOL
        k32.ReleaseMutex.argtypes = [wintypes.HANDLE]
        self._k32 = k32
        self._handle = k32.CreateMutexW(None, False, name)
        if not self._handle:
            raise OSError("CreateMutexW 失败(%s)" % name)

    def acquire(self, timeout):
        WAIT_OBJECT_0, WAIT_ABANDONED, WAIT_TIMEOUT = 0x0, 0x80, 0x102
        ms = int(max(0.0, timeout) * 1000)
        r = self._k32.WaitForSingleObject(self._handle, ms)
        if r in (WAIT_OBJECT_0, WAIT_ABANDONED):
            return
        if r == WAIT_TIMEOUT:
            raise DBInitError("等待微信数据库构造锁超过 %.0fs" % timeout)
        raise OSError("WaitForSingleObject 返回 %s" % r)

    def release(self):
        self._k32.ReleaseMutex(self._handle)


class _NullMutex:
    """非 Windows / Mutex 不可用时的退化实现。"""

    def acquire(self, timeout):
        pass

    def release(self):
        pass


_mutex = None
_mutex_guard = threading.Lock()
_rlock = threading.RLock()       # 进程内：单例创建 + 排队
_db = None                       # 本进程唯一的 WeChatDB 实例


def _get_mutex():
    global _mutex
    if _mutex is None:
        with _mutex_guard:
            if _mutex is None:
                if sys.platform == "win32":
                    try:
                        _mutex = _NamedMutex(MUTEX_NAME)
                    except Exception as e:      # noqa: BLE001
                        print("[db] 跨进程构造锁不可用，退化为进程内互斥: %s" % e)
                        _mutex = _NullMutex()
                else:
                    _mutex = _NullMutex()
    return _mutex


@contextlib.contextmanager
def hold(what="", timeout=DEFAULT_TIMEOUT):
    mx = _get_mutex()
    mx.acquire(timeout)
    try:
        yield
    finally:
        mx.release()


# ---------------------------------------------------------------- 私有副本目录
def _proc_workdir(account):
    return os.path.join(tempfile.gettempdir(), "wechatauto_db",
                        account, SUBDIR_PREFIX + str(os.getpid()))


def _purge_middle(work):
    """清掉本进程私有目录里的中间产物，保留已验证的 .db 与 .stamp。

    只动 proc_<自己pid> 目录：别的进程正在用的文件一个都不碰。
    PID 会被系统复用，上次异常退出留下的 .tmp/-shm/-wal 会挡住这次的
    os.replace，所以每次进程首次建库都先扫一遍。
    """
    patterns = ("*.tmp*", "*-shm", "*-wal", "*.tmp.wal*")
    seen = set()
    for pat in patterns:
        for p in glob.glob(os.path.join(work, pat)):
            if p in seen:
                continue
            seen.add(p)
            try:
                if os.path.isfile(p):
                    os.remove(p)
            except OSError:
                pass                      # 正被占用就跳过，重试时再清


def _is_win_locked(e):
    """是不是 Windows 文件占用类错误（WinError 5 / 32 / 33）。"""
    if getattr(e, "winerror", None) in (5, 32, 33):
        return True
    s = str(e)
    return any(k in s for k in ("拒绝访问", "WinError 5", "Access is denied",
                                "另一个程序正在使用此文件",
                                "being used by another process"))


def _new_db():
    """惰性导入：无 wechatauto 的环境里本模块仍可被 import（看板容错）。"""
    from wechatauto import WeChatDB
    return WeChatDB()


def _build():
    """构造一个 WeChatDB，并把它的缓存/密钥全部搬到本进程私有目录。"""
    with hold("初始化微信数据库"):
        db = _new_db()
        work = _proc_workdir(db.account)
        os.makedirs(work, exist_ok=True)
        private_keys = os.path.join(work, "keys.json")
        src_keys = db.keys_file
        if (src_keys and os.path.exists(src_keys)
                and os.path.abspath(src_keys) != os.path.abspath(private_keys)):
            try:
                shutil.copyfile(src_keys, private_keys)
            except OSError as e:
                print("[db] 复制密钥缓存失败（将回退到内存提取）: %s" % e)
        # 覆盖成私有目录：之后 _open_unlocked 的 dst / stamp / tmp 都落这里，
        # _save_keys 也只写这里，和别的进程再无文件级交集。
        db.workdir = work
        db.keys_file = private_keys
    _purge_middle(work)
    print("[db] 解密缓存隔离目录: %s" % work)
    return db, work


def get_db(reuse=True):
    """取得本进程的 WeChatDB 实例（默认单例）。

    预热一次 contact 库：WinError 5 最容易在第一次解密时暴露，早失败早自愈，
    也避免错误拖到收消息轮询里变成一堆看不懂的堆栈。
    """
    global _db
    with _rlock:
        if reuse and _db is not None:
            return _db
        db, work = _build()
        for attempt in (1, 2, 3):
            try:
                db.get_self_info()      # 触发 contact 库解密/重建
                break
            except Exception as e:      # noqa: BLE001
                if not _is_win_locked(e) or attempt == 3:
                    raise
                print("[db] 解密缓存被占用，清理本进程中间产物后重试 %d/2: %s"
                      % (attempt, e))
                _purge_middle(work)
                time.sleep(0.6 * attempt)
        if reuse:
            _db = db
        return db


def reset():
    """丢弃进程内单例（仅用于诊断/强制重建，正常路径不要调）。"""
    global _db
    with _rlock:
        _db = None


def cleanup_stale(max_age_hours=48):
    """手动清理其他进程遗留的私有副本目录（显式调用，不自动删）。

    用法：python -c "import db_gate; print(db_gate.cleanup_stale())"
    """
    root = os.path.join(tempfile.gettempdir(), "wechatauto_db")
    mine = SUBDIR_PREFIX + str(os.getpid())
    removed = []
    if not os.path.isdir(root):
        return removed
    cutoff = time.time() - max_age_hours * 3600
    for acct in os.listdir(root):
        base = os.path.join(root, acct)
        if not os.path.isdir(base):
            continue
        for name in os.listdir(base):
            p = os.path.join(base, name)
            if (not name.startswith(SUBDIR_PREFIX) or name == mine
                    or not os.path.isdir(p)):
                continue
            try:
                if os.path.getmtime(p) > cutoff:
                    continue
                shutil.rmtree(p)
                removed.append(p)
            except OSError:
                pass
    return removed
