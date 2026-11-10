#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
个人微信 客户维护 —— PC 端「键盘驱动」自动化骨架（非注入）
=====================================================================
为什么是「键盘驱动」而不是「控件驱动」：
- 经实测，你装的微信 PC 是 Qt 渲染版（窗口类 Qt51514QWindowIcon / 子窗口
  MMUIRenderSubWindowHW）。整個界面是微信自己用 Qt 画的，**不暴露成 Windows
  原生控件**（没有 Edit / Button 这些），所以 uiautomation / pywinauto 按名字/
  类名找"搜索框/输入框/发送按钮"这条路天然走不通（ui_dump.txt 只有 3 行即为证）。
- 于是改为「键盘驱动」：只给前台微信窗口发快捷键，不找任何控件。
  流程：聚焦微信 → Ctrl+F 打开搜索 → Ctrl+V 粘贴名字 → Enter 打开会话
        → Ctrl+V 粘贴消息 → Enter 发送。
- 同样「驱动官方 App、不注入、不逆向」，与你担心的 wcferry 注入式封号无关。
- 剩下的封号风险只来自「行为风控」（加人速度 / 消息雷同 / 频率），与方式无关，
  只能靠 限速 + SOP 压。

前置（Windows + 已登录微信 PC 端）：
1. 手机扫码登录并打开微信 Windows 客户端，保持前台、不锁屏。
2. pip install -r requirements.txt
3. 编辑 config.json：联系人（name 用「微信里能搜到且唯一」的名字/备注）、模板、任务。
   （脚本只识别个人「微信」窗口，自动排除「企业微信」「微信开发者工具」等。）
4. 先 dry-run：python wechat_ui.py --send --template welcome --tag new
   （dry-run 只打印按键序列，不真发；但仍会校验微信窗口是否存在）
5. 真发：加 --no-dry。例如
   python wechat_ui.py --send --template welcome --tag new --no-dry

⚠️ 关键前提（务必先确认）：
- 微信 PC 的「发送消息」需为「Enter 发送」（默认即是）。若你改成了 Ctrl+Enter，
  本骨架需相应调整（见 _SEND_KEY）。
- 搜索框需能接受 Ctrl+V 粘贴（支持）。名字请用「备注名」等唯一字，重名会点中第一个结果。
- 加好友：PC 微信（尤其 Qt 版）普遍无法稳定发出好友申请，本骨架 --add-friend 仅作
  提示并建议回手机端，不强行模拟（避免误操作和风控）。

依赖：pyautogui（发键）、pyperclip（剪贴板，用于输入中文）、APScheduler（定时）。
"""
import argparse
import json
import logging
import re
import time
import ctypes
from ctypes import wintypes

import pyautogui
import pyperclip
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

import ui_gate
from ai_client import generate as ai_generate
from monitor_store import add_message as store_add_message
from monitor_store import add_sent as store_add_sent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("wechat_pc.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("wechat_pc")

CONFIG_PATH = "config.json"

# 发消息用的「发送键」。默认 Enter；若微信设成 Ctrl+Enter 发送，改成 ("ctrl", "enter")
_SEND_KEY = "enter"


def normalize_probe(text):
    """比对输入框内容 / 微信库消息时用的归一化：抹掉所有空白字符。

    为什么必须有这一步（2026-11-10「人工接管通知只粘贴、不发送」事故）：
    接管通知、活动文案是**多行**的，`message.strip()[:16]` 往往正好切在
    换行符上；而 Windows 剪贴板 Ctrl+A / Ctrl+C 回读会把换行变成 CRLF，
    微信本地库里存的又可能是另一种换行。直接拿原始 probe 做子串比对，
    会把「正文明明已经粘进输入框」误判成「没粘进去」→ 抛 SendAborted →
    **不按发送键**，文案就一直留在聊天输入框里。
    两边都抹掉空白（空格/制表/LF/CR）后再比对，就与换行形态无关了。
    """
    return re.sub(r"\s+", "", text or "")


class SendNotVerified(Exception):
    """键盘流程跑完了，但回去查微信库没看到这条消息 —— 判定「没真正发出」。

    为什么需要这个异常（2026-10-19 实测事故）：Qt 渲染版微信用键盘驱动发送，
    Enter 可能按在一个**没聚焦的输入框**上，全程不抛任何异常；调用方于是以为
    发送成功，看板写了「已发送」、任务标 sent，而微信客户端里什么都没有。
    既然读不到输入框状态，唯一可靠的判据就是**回读微信库**。
    """


class SendAborted(Exception):
    """还没到「发送」这一步就主动中止了 —— 一定**没发出任何东西**。

    2026-10-21 用户报的场景：Ctrl+F 之后昵称没真进搜索框，程序却继续往下走，
    粘了 AI 正文、按了回车，结果**根本没进到目标会话**，正文要么发给了当前
    打开的那个会话（发错人），要么卡在搜索框里。

    根因是没有一步验证「昵称确实输进去了」。现在 `_open_conversation` 用
    「Ctrl+A + Ctrl+C 回读焦点框文本」来确认，确认不到就**整条中止**：
    不粘正文、不按发送键，直接抛这个异常。

    和另外两个的区别：SendNotVerified＝发了但库里查不到；SendUnconfirmed＝
    发没发不知道；SendAborted＝**确定什么都没发**，可以直接重试或交人工。
    """


class SendUnconfirmed(Exception):
    """消息可能发出去了，但**没法确认**（回读微信库不可用）。

    和 SendNotVerified 的区别：那个是「查了，确实没有」；这个是「查不了，
    不知道有没有」。处理方式也不同——
      · 不写库：看板不能凭空多一条没证据的消息（用户口径：真实发出才落库）。
      · 不重试：可能已经发出去了，重发就是给客户发两遍（发送不可逆，更糟）。
    结果就是任务落 failed + reason 标「无法确认」，留给人工核对。
    """

# 搜索/输入之间的安全间隔（秒），太短微信吃不到按键
_STEP_GAP = 0.4

pyautogui.FAILSAFE = True   # 鼠标甩到屏幕角落可紧急中止
pyautogui.PAUSE = 0.05

# ---------- 窗口定位（ctypes，不依赖 UIA / 控件树）----------
_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

# 浏览器/系统容器类名：标题含「微信」的网页（如看板页面「微信客户维护·看板」）
# 也曾被误判成微信主窗口，导致快捷键全发进浏览器。见到这些类名一律排除。
_BROWSER_CLASSES = ("Chrome_WidgetWin", "MozillaWindowClass",
                    "ApplicationFrame", "Windows.UI.Core", "EE_Window")
# 微信主窗口的 Qt 类名（实测 Qt51514QWindowIcon；微信升级可能变）。
# 它是**主判据**：开着会话时主窗标题会变成联系人名，标题不可靠，
# 类名才是稳定标识（2026-10-16 focus_wechat 因标题变化找不到窗口）。
_WECHAT_CLASS_HINT = "Qt515"
VK_MENU = 0x12           # Alt 键，用于解除 Windows 前台锁
KEYEVENTF_KEYUP = 0x0002
SW_RESTORE = 9


def _class_of(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _proc_path(hwnd):
    """窗口所属进程的可执行路径（查询失败返回空串，如权限不足）。"""
    try:
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return ""
        h = _kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(260)
            n = wintypes.DWORD(260)
            if _kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
                return buf.value
        finally:
            _kernel32.CloseHandle(h)
    except Exception:
        pass
    return ""


def find_wechat_windows():
    """返回 [(hwnd, title), ...]，只认个人微信主窗口。
    双保险防误判（历史事故：标题含「微信」的看板浏览器窗口被当成微信，
    快捷键 Ctrl+F/粘贴/Enter 全发进浏览器）：
    ① 类名含 Qt515（微信 Qt 主窗口）——**不依赖标题**：开着会话时主窗
      标题会变成联系人名（2026-10-16 实测 focus_wechat 因此找不到窗口），
      类名才是稳定标识；
    ② 或标题精确等于「微信」；
    类名是浏览器/系统容器的一律先排除；标题含「企业」「开发者」的一律排除。
    多个命中时排序：Weixin.exe 进程优先 → 精确标题「微信」优先 → 其余。
    """
    found = []

    def _enum(hwnd, _lparam):
        if not _user32.IsWindowVisible(hwnd):
            return
        length = _user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return
        buf = ctypes.create_unicode_buffer(length + 1)
        _user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value
        if ("企业" in title) or ("开发者" in title):
            return
        cls = _class_of(hwnd)
        if any(b in cls for b in _BROWSER_CLASSES):
            return                      # 看板/网页窗口，不是微信
        if (_WECHAT_CLASS_HINT in cls) or title == "微信":
            found.append((hwnd, title))

    ENUMPROC = ctypes.WINFUNCTYPE(None, wintypes.HWND, wintypes.LPARAM)
    _user32.EnumWindows(ENUMPROC(_enum), 0)

    def _rank(t):
        hwnd, title = t
        proc = _proc_path(hwnd).replace("/", "\\").lower()
        is_weixin = proc.endswith("\\weixin.exe")
        return (0 if is_weixin else 1, 0 if title == "微信" else 1)

    found.sort(key=_rank)
    return found


def _activate(hwnd):
    """把指定窗口强制激活到真正前台。Windows 前台锁下裸 SetForegroundWindow
    会静默失败（只闪任务栏），所以用组合技：最小化先还原 → Alt tap 解除前台限制 →
    AttachThreadInput 附到前台线程 → SetForegroundWindow → 轮询确认（最多 3s）。"""
    if _user32.IsIconic(hwnd):
        _user32.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.2)
    if _user32.GetForegroundWindow() == hwnd:
        return True
    # Alt tap：让系统认为本进程刚收到用户输入，解除「只有前台进程能设置前台」的限制
    _user32.keybd_event(VK_MENU, 0, 0, 0)
    _user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
    # 附加到当前前台线程的输入队列，联合设置前台
    fg = _user32.GetForegroundWindow()
    fore_tid = _user32.GetWindowThreadProcessId(fg, None)
    my_tid = _kernel32.GetCurrentThreadId()
    if fore_tid and fore_tid != my_tid:
        _user32.AttachThreadInput(my_tid, fore_tid, True)
    try:
        _user32.SetForegroundWindow(hwnd)
        _user32.BringWindowToTop(hwnd)
    finally:
        if fore_tid and fore_tid != my_tid:
            _user32.AttachThreadInput(my_tid, fore_tid, False)
    for _ in range(30):
        if _user32.GetForegroundWindow() == hwnd:
            time.sleep(0.15)   # 等窗口完成绘制、焦点落稳再发键
            return True
        time.sleep(0.1)
    return False


def _rejected_candidates_desc():
    """报错时附上「像微信但被排除」的可见窗口，辅助排查（只读）。"""
    seen = []

    def _enum(hwnd, _lparam):
        if not _user32.IsWindowVisible(hwnd):
            return
        n = _user32.GetWindowTextLengthW(hwnd)
        if n == 0:
            return
        buf = ctypes.create_unicode_buffer(n + 1)
        _user32.GetWindowTextW(hwnd, buf, n + 1)
        title = buf.value
        cls = _class_of(hwnd)
        if "微信" in title or _WECHAT_CLASS_HINT in cls:
            seen.append((title, cls))

    ENUMPROC = ctypes.WINFUNCTYPE(None, wintypes.HWND, wintypes.LPARAM)
    _user32.EnumWindows(ENUMPROC(_enum), 0)
    if not seen:
        return ""
    return " 当前可见的疑似窗口：" + "; ".join(
        "%r(%s)" % (t[:30], c) for t, c in seen[:5])


def _foreground_desc():
    fg = _user32.GetForegroundWindow()
    n = _user32.GetWindowTextLengthW(fg)
    buf = ctypes.create_unicode_buffer(n + 1)
    _user32.GetWindowTextW(fg, buf, n + 1)
    return buf.value


def focus_wechat(retries=3, gap=1.0):
    """把个人微信主窗口激活到前台并返回 (hwnd, title)。找不到/激活失败则抛错。

    retries：页面切换/树物化期间窗口枚举可能**瞬时**失败（2026-10-16 实测：
    切回聊天页后立刻枚举不到窗口，而微信明明开着），重试几次再认输。
    """
    err = ""
    for i in range(max(1, int(retries))):
        wins = find_wechat_windows()
        if wins:
            hwnd, title = wins[0]
            if _activate(hwnd):
                return hwnd, title
            err = ("无法把微信窗口激活到前台（当前前台是 %r）。"
                   "可能原因：微信刚启动未就绪 / 系统前台限制。"
                   "请手动点一下微信窗口再重试。" % _foreground_desc())
        else:
            err = ("找不到个人微信窗口（已自动排除企业微信/浏览器页面）。"
                   "请确认个人微信已登录并打开；可用 --inspect 查看当前识别到的窗口。"
                   + _rejected_candidates_desc())
        if i < retries - 1:
            time.sleep(gap)
    raise RuntimeError(err)


def wechat_is_foreground():
    """当前前台窗口是否就是个人微信主窗（Qt515 类名或标题精确「微信」）。

    用于「已在前台就别再激活」——refresh 刚聚焦过时重复枚举会撞上瞬时
    竞态（2026-10-16：切回聊天页后立刻枚举不到窗口，其实前台就是微信）。
    """
    fg = _user32.GetForegroundWindow()
    if not fg:
        return False
    cls = _class_of(fg)
    if any(b in cls for b in _BROWSER_CLASSES):
        return False
    if _WECHAT_CLASS_HINT in cls:
        return True
    n = _user32.GetWindowTextLengthW(fg)
    buf = ctypes.create_unicode_buffer(n + 1)
    _user32.GetWindowTextW(fg, buf, n + 1)
    return buf.value == "微信"


def _require_foreground(hwnd):
    """非 dry-run 时，确认前台确实是我們聚焦的微信窗口；否则中止本次发送，
    避免按键发到别的窗口（历史上出过两类事故：误关窗口、快捷键发进看板浏览器）。"""
    if _user32.GetForegroundWindow() != hwnd:
        fg = _user32.GetForegroundWindow()
        n = _user32.GetWindowTextLengthW(fg)
        buf = ctypes.create_unicode_buffer(n + 1)
        _user32.GetWindowTextW(fg, buf, n + 1)
        raise RuntimeError(
            "微信未成为前台窗口（当前前台是 %r），已中止本次发送以防按键发错窗口。"
            "请先手动点一下微信窗口，再重跑脚本。" % buf.value
        )


def switch_to_chat_page(require_list=True, settle=1.0, max_clicks=2):
    """把微信主窗带回聊天页（控件树点左侧导航「微信」tab，复用库的 UIA 路线）。

    为什么必须有：键盘发送流程（Ctrl+F 搜人 → 粘贴 → Enter）**只在聊天页
    有效**。客户端停在朋友圈页时，会话列表/搜索框整片不渲染，Ctrl+F 直接
    失效、后续步骤全废（2026-10-16 实测：朋友圈刷新后/用户手动逛朋友圈后
    发送全挂）。库作者同样踩到——wechatauto 的 SwitchToMoments 会把主窗
    留在朋友圈页，其 uia_driver.back_to_chat_tab 注释原话：「之后一切按
    搜索框/消息列表走的操作静默失败」。所以不自己造轮子，直接调库。

    require_list=True：保证回到**会话列表**（窄窗口开着会话时要点两次才退出
    会话，库里已处理）；False 只保证回聊天页、保留原打开会话（备用能力，
    当前流程（发消息前）只用默认 True）。

    best-effort：失败只打印返回 False，不抛——调用方继续，真发送时随后的
    _require_foreground + 定位失败会兜底暴露。
    """
    try:
        from wechatauto.uia_driver import WeChatUIA
        uia = WeChatUIA()
        if not uia.ensure_window():
            print("[ui] 切回聊天页失败：找不到微信主窗口或 UIA 树未物化")
            return False
        ok = uia.back_to_chat_tab(settle=settle, max_clicks=max_clicks,
                                  require_list=require_list)
        if ok:
            print("[ui] 已通过控件树切回聊天页")
        else:
            print("[ui] 切回聊天页未确认（会话列表未出现），仍继续尝试发送")
        return ok
    except Exception as e:
        print("[ui] 切回聊天页异常（不影响本次发送尝试）: %s" % e)
        return False


def load_config(path: str = CONFIG_PATH) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


class WeChatPCBot:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.dry_run = cfg.get("dry_run", True)
        self.delay = cfg.get("send_delay_seconds", 3)

    # ---------- 底层：键盘驱动 ----------
    def _press(self, key):
        if self.dry_run:
            return
        pyautogui.press(key)
        time.sleep(_STEP_GAP)

    def _hotkey(self, *keys):
        if self.dry_run:
            return
        pyautogui.hotkey(*keys)
        time.sleep(_STEP_GAP)

    def _paste(self, text):
        """用剪贴板粘贴（支持中文；pyautogui.write 无法输入中文）。"""
        if self.dry_run:
            return
        pyperclip.copy(text)
        pyautogui.hotkey("ctrl", "v")
        time.sleep(_STEP_GAP)

    # 剪贴板哨兵：不要用 \x00 之类的字符——Windows 剪贴板按 NUL 截断，
    # 写进去会变成空串，哨兵就失去意义了。
    _CLIP_SENTINEL = "__wechatbot_clip_sentinel__"

    def _read_edit_text(self):
        """把**当前焦点输入框**里的文本复制出来读，读不到返回 ""。

        为什么需要（2026-10-21）：微信是 Qt 自绘，控件树读不到，但输入框本身支持
        标准的 Ctrl+A / Ctrl+C。于是「昵称到底有没有输进去」这件事可以客观验证：
        先往剪贴板塞一个哨兵值，再全选复制，最后读剪贴板——
          · 读回哨兵 → 复制没生效（焦点不在可编辑控件上，或框里本来就是空的）；
          · 读回别的 → 那就是焦点框里的真实内容。
        哨兵不能省：否则复制失败时读到的是上一轮留在剪贴板里的旧内容（很可能正是
        要搜的昵称），会把「什么都没输进去」误判成「输进去了」。

        纯只读：Ctrl+A 只是**选中**，不删不写；紧接着的粘贴会整体替换选中内容，
        这正是我们要的（不让上一次的残留文字混进来）。
        """
        if self.dry_run:
            return ""
        sentinel = self._CLIP_SENTINEL
        try:
            pyperclip.copy(sentinel)
        except Exception:               # noqa: BLE001
            return ""
        pyautogui.hotkey("ctrl", "a")
        time.sleep(0.15)
        pyautogui.hotkey("ctrl", "c")
        time.sleep(0.25)
        try:
            got = pyperclip.paste() or ""
        except Exception:               # noqa: BLE001
            return ""
        return "" if got == sentinel else got

    def _open_conversation(self, name, tries=3):
        """Ctrl+F 搜昵称 → 进会话，并且**每一步都回读验证真的生效**。返回 bool。

        2026-10-21 用户报：「有时候没把昵称输入进去，就粘贴了 AI 内容并回车确认，
        导致根本没进入会话窗口发送」。原流程 Ctrl+F → Ctrl+A → 粘贴 → 回车 全是
        「发完就不管」：微信搜索面板是异步渲染的，慢一点那次粘贴就落空，后面
        回车变成在空搜索框上按回车，AI 正文则粘进了**当前打开的那个会话**——
        要么发错人，要么卡在搜索框里没发出去。

        现在：
          1. 最多试 tries 次：Ctrl+F → 全选 → 粘昵称 → **回读焦点框**（见
             `_read_edit_text`）。读到昵称才算成功，读不到就重来（面板还没起来
             / 焦点没进搜索框）——用户遇到的就是「这一步悄悄失败」。
          2. 按 Enter 打开搜索结果，再回读焦点框：
             · 空 → 已在消息输入框，可以往下发；
             · 仍**全等**于昵称 → 卡在搜索框（Enter 没打开会话：重名、无结果等），
               再按一次 Enter 试一次，仍不行就放弃；
             · 其他内容 → 输入框里有草稿，照发（粘贴会覆盖它，记一条日志）。
        返回 False 时调用方必须**整条中止**，绝不能继续粘正文。
        """
        target = (name or "").strip()
        if not target:
            log.error("搜索词为空，拒绝执行 Ctrl+F 流程（避免把正文发到当前会话）")
            return False
        for i in range(max(1, tries)):
            self._hotkey("ctrl", "f")       # 打开搜索
            time.sleep(0.5)                 # 等面板起来并拿到焦点（异步渲染）
            self._hotkey("ctrl", "a")       # 全选搜索框旧内容
            self._paste(target)             # 粘贴要搜的名字
            time.sleep(0.3)
            got = self._read_edit_text()
            if (got or "").strip() == target:
                break
            log.warning("Ctrl+F 后昵称没进搜索框（回读到 %r），重试 %d/%d",
                        (got or "")[:30], i + 1, tries)
            time.sleep(0.4)
        else:
            log.error("Ctrl+F 搜索「%s」连续 %d 次没把昵称输进去，中止本次发送", target, tries)
            return False

        self._press("enter")                # 打开顶部搜索结果（需名字唯一）
        time.sleep(0.7)
        got = (self._read_edit_text() or "").strip()
        # 判据用**全等**而不是「包含」：搜索框里就是我们刚粘的那串昵称，全等才好认。
        # 若用包含，输入框里恰好有一句提到该昵称的草稿时会被误判成「还在搜索框」，
        # 再按一次回车就把草稿发出去了。
        if got == target:
            # 还停在搜索框：Enter 没打开会话。再给一次机会（列表项可能还没渲染完）
            self._press("enter")
            time.sleep(0.7)
            got = (self._read_edit_text() or "").strip()
            if got == target:
                log.error("Enter 没能打开「%s」的会话（搜索框里仍是昵称），中止本次发送",
                          target)
                return False
        elif got:
            log.warning("「%s」的会话输入框里已有未发内容（%r），本次发送会覆盖它",
                        target, got[:30])
        return True

    def _type_at(self):
        """敲一个真的「@」键（不经过剪贴板）。

        为什么不能用 `_paste`（2026-10-21 用户报「群里先 @ 发一条、正文再发一条」）：
        微信 PC 的成员候选弹窗只响应**键盘敲出来的 @**。用剪贴板 Ctrl+V 粘进去的
        「@」在它看来只是一个普通字符，弹窗根本不弹。弹窗不在的时候，`_at_member`
        里那个回车就没有「选中成员」的含义了，直接等于发送 —— 这正是原来「@ 单独
        占一个气泡」的原因。换成真实按键后弹窗正常出现，回车=选中成员（不发送），
        整条消息由调用方再做一次发送。
        """
        if self.dry_run:
            return
        pyautogui.write("@", interval=0.05)
        time.sleep(_STEP_GAP)

    def _at_member(self, name, message):
        """群聊：把「@昵称 + 正文」合进**同一条消息**发出（2026-10-21 用户口径）。

        做法是「**先写正文，再把 @ 补到最前面**」，而不是「先 @ 再写正文」：
          · 先 @ 再写正文时，@ 之后那个回车本来是用来**选中**成员的，但微信的
            成员候选弹窗是 Qt 自绘、读不到也确认不了有没有弹出来。弹窗没弹的
            时候，这个回车就等于**发送** ——「@昵称」先成一条，正文再成第二条
            （2026-10-21 用户截图正是这个现象）。
          · 反过来先粘正文、Ctrl+Home 回行首再敲 @，两种结果都只剩一条：
            弹窗在 → 回车=选中成员（不发送），再回车才发，消息是「@昵称 正文」；
            弹窗不在 → 回车直接把「@昵称 正文」整条发出去，紧接着那次回车落在
            空输入框上，微信不会发空消息。
        代价：Ctrl+Home 万一不被输入框支持，@ 会落在正文末尾（仍是同一条，
        内容不丢，只是顺序不对）。

        空格怎么来的：正文是**带一个前导空格**粘进去的，@昵称 补在最前面之后
        自然就是「@昵称 正文」。昵称自己后面不能加空格——那会一起进候选过滤，
        微信按完整串匹配，「张三 」匹配不到「张三」，弹窗反而选不中。

        为什么要用真实按键敲 @：微信的成员候选弹窗只认**键盘输入的 @**，用剪贴板
        Ctrl+V 粘进去的「@」只是个普通字符，弹窗根本不弹（这是原来「@ 单独成一条」
        的直接原因）。见 `_type_at`。
        """
        if self.dry_run:
            return
        self._paste(" " + message)      # 正文前预留一个空格：@{昵称} 补进来正好隔开
        self._hotkey("ctrl", "home")    # 光标回行首：@ 必须加在正文前面
        time.sleep(0.2)
        self._type_at()                 # 真实按键才弹成员列表
        time.sleep(0.9)                 # 等弹窗（异步渲染）
        # 昵称后面**不能**再加空格：那会一起进候选过滤，微信按完整串匹配，
        # 「张三 」匹配不到「张三」→ 弹窗反而选不中。空格已经预留在正文前面了。
        self._paste(name)               # 完整群昵称，让候选收敛到唯一匹配
        time.sleep(0.9)                 # 等候选过滤收敛
        self._press("enter")            # 弹窗在=选中成员；不在=整条发出去
        time.sleep(0.3)
        self._send_keystroke()          # 弹窗在时这一下才真正发送；不在时输入框已空

    def _send_keystroke(self):
        if self.dry_run:
            return
        if _SEND_KEY == "enter":
            pyautogui.press("enter")
        else:
            pyautogui.hotkey(*_SEND_KEY)
        self._rhythm_nap()

    def _rhythm_nap(self):
        """发送后等待：rhythm.nap 只让等待变长（×1.0~1.45 随机），不会比固定
        delay 更快——拿稳定性换抖动不划算。库不可用时回退固定 sleep。"""
        try:
            from wechatauto import rhythm
            rhythm.nap(self.delay)
        except Exception:
            time.sleep(self.delay)

    # ---------- 业务动作 ----------
    def send_to_contact(self, name, message, conv_id=None, ui_timeout=None, by="manual",
                        confirm=None, at_name=None):
        """对一个联系人：搜索→进会话→发消息。execute 由 dry_run 控制。
        conv_id 传入微信 wxid 时，看板消息库按 wxid 归并会话（与收消息桥一致）；
        省略时按 name 归并（手动 CLI 发送的兼容行为）。
        by：写回消息库时标注发送方——'ai'（auto_reply 自动回复）/ 'manual'
        （看板或 CLI 人工发）。看板据此在气泡旁打 AI/人工 小标。
        ui_timeout：在 UI 门上排队等待的上限（秒）。调用方传「本条任务的剩余
        时间预算」（auto_reply 的任务级超时），省略则用全局默认
        （config.ui.lock_timeout_seconds）。
        confirm：**发送结果校验回调** `confirm(conv_id, message) -> bool`。
        键盘动作跑完不等于消息真的出去了（Enter 可能按在没聚焦的输入框上，
        全程不抛异常）。传了回调就**先校验、通过才写库**；校验不过抛
        SendNotVerified，调用方据此把任务落 failed，而不是记一条假成功。
        回读数据库由调用方负责，本函数不持有连接。
        at_name：**群聊专用**——把「@昵称 」和正文合进**同一条消息**发出（群里
        要让人看出在回谁，但用户不想要「先 @ 一条、正文再一条」）。None 表示
        私聊或不需要 @。两条路径都只产生一个气泡，见 `_at_member` 的说明。

        **整个键盘流程在 UI 门内串行执行**（2026-10-16 并发加固）：微信只有
        一份 UI，auto_reply 自动回复与看板手动发送是**两个进程**，不加跨进程
        互斥就会互相抢前台（按键发错窗口 / 朋友圈定位在聊天页白跑）。拿不到
        门就排队等，不丢弃；等超时抛 UITimeoutError 交给调用方报错，不静默吞。
        键盘序列本身（聚焦→切页→Ctrl+F→粘贴→Enter→等待发送节流）全部在门内，
        包括发送后的 rhythm 节流等待——那段时间微信仍应独占前台，放出去会让
        两条外发贴太近（风控）。
        """
        plan = (
            f"聚焦微信窗口 → Ctrl+F 搜索 → 粘贴{name!r} → Enter 进会话 "
            f"→ 粘贴消息 → {_SEND_KEY} 发送 → 等待 {self.delay}s"
        )
        if self.dry_run:
            log.info("[DRY-RUN] 计划发送给 %s : %s", name, message)
            log.info("[DRY-RUN] 按键序列: %s", plan)
            return
        # 群里 @ 之后真正落屏的那条消息是「@昵称 正文」（同一个气泡）。sent_log
        # 与看板消息库都按**真实外发文本**记，否则收消息桥认不出来又把这条自己
        # 发的消息补录一遍，看板同一句出现两条。
        sent_text = ("@" + at_name + " " + message) if at_name else message
        try:
            # 先在**门外**、且在按发送键**之前**记 sent_log：收消息轮询读到这条
            # 回执时要靠它去重跳过。必须在按键前写——发送后本函数还要 nap(delay)
            # ≈5s 才写看板库，而收消息轮询跑在另一个线程、并发进行；若等发送后
            # 再写 sent_log，轮询就会在这 5s 空窗里把回执当成新消息再入库一条
            # （2026-10-16 用户看到同一句话在会话里出现两遍：收桥那条 id 更小、
            # 时间更早，发送那条 id 更大、晚 7s）。写在这里也顺便保证这次 DB 写
            # 不在 UI 门内（不占着微信锁做 IO）。
            try:
                store_add_sent(conv_id or name, sent_text)
            except Exception as e:
                log.warning("预写 sent_log 失败（回执可能重复上屏）: %s", e)
            with ui_gate.hold("发送给 %s" % name, timeout=ui_timeout):
                hwnd, title = focus_wechat()
                _require_foreground(hwnd)   # 关键：确保按键只发到微信，发错就中止
                # 关键：Ctrl+F 只在聊天页有效（停在朋友圈页会静默失效）。
                # 返回 False 只是「会话列表没确认出来」，不代表一定在别的页，
                # 所以**不中止**（这条判据本身是 best-effort，硬中止会让所有
                # 回复一起停摆）；但多给它一次机会，切回来了 Ctrl+F 才靠得住。
                if not switch_to_chat_page():
                    log.warning("切回聊天页未确认，再试一次（%s）", name)
                    time.sleep(0.5)
                    switch_to_chat_page()
                _require_foreground(hwnd)   # 切页后再确认一次前台（双保险，历史事故教训）
                # 搜索并进入会话。**必须验证昵称真的输进去了**——否则后面粘贴的
                # 正文会落到当前打开的会话里（2026-10-21 用户报的发错会话）。
                if not self._open_conversation(name):
                    raise SendAborted(
                        "没能进入「%s」的会话（Ctrl+F 搜索框里没读到昵称，或 Enter "
                        "没打开搜索结果）。已在粘贴正文之前中止，本次没有发出任何内容。"
                        % name)
                if at_name:
                    # 群聊：@ + 正文合并成同一条（内部含最后一次发送）
                    self._at_member(at_name, message)
                else:
                    self._paste(message)    # 粘贴消息到会话输入框
                    # 粘完再回读一次：确认正文**真的进了输入框**才按发送键。
                    # 用前 16 个字做比对（不是「非空就算过」）——焦点万一跑到聊天
                    # 记录区，Ctrl+A/Ctrl+C 复制到的是聊天内容，非空判断会放过它。
                    # 两边都走 normalize_probe：多行通知的 LF / 剪贴板回读的 CRLF
                    # 必须算同一份文本，否则会误判成「没粘进去」而不敢按发送键。
                    probe = normalize_probe(message)[:16]
                    if probe and probe not in normalize_probe(self._read_edit_text()):
                        raise SendAborted(
                            "正文没粘进「%s」的输入框，未按发送键（本次没有发出任何内容）"
                            % name)
                    self._send_keystroke()  # 发送
            # 注意：这里**不**打「已发送」——键盘动作跑完只代表按了键，
            # 真正算不算发出去要看下面的回读校验。日志必须与真实结果一致，
            # 否则排查时会被一行"已发送"带偏。
            # 发送结果校验：键盘序列跑完 ≠ 消息真出去了（见 SendNotVerified）。
            # 放在 UI 门外做——回读数据库不占微信锁，且此时按键已经完成，别人
            # 抢前台也不会把这条已排进微信发送队列的消息弄丢。
            # 三态：True=确认到 → 写库；False=查了没有 → 未发出；None=查不了
            # → 无法确认（既不写库也不重发，交人工核对）。
            if confirm is not None:
                try:
                    verified = confirm(conv_id or name, message)
                except Exception as e:      # noqa: BLE001
                    log.warning("发送校验回调异常，按「无法确认」处理: %s", e)
                    verified = None
                if verified is None:
                    raise SendUnconfirmed(
                        "回读微信库不可用，无法确认是否发出：%r" % (message[:24],))
                if not verified:
                    raise SendNotVerified(
                        "微信库里没查到这条消息，按未发出处理：%r" % (message[:24],))
            # 写回统一消息库，供网页看板即时上屏（失败不影响已发送；不碰 UI，
            # 放在门外做，别白占 UI 锁）
            try:
                store_add_message(conv_id or name, "me", sent_text, name=name, sent_by=by)
            except Exception as e:
                log.warning("写入看板消息库失败（不影响已发送）: %s", e)
            # 到这里才是「回读确认过 + 已落库」，日志和账本含义一致
            log.info("已发送并确认(%s)", name)
        except ui_gate.UITimeoutError as e:
            # 排队超时＝有别的操作长时间占着微信。不吞：抛给调用方
            # （看板手动发送要报错，自动回复要记日志本轮跳过）。
            log.error("等待微信 UI 空闲超时，已取消本次发送(%s): %s", name, e)
            raise
        except SendNotVerified as e:
            # 这一步**必须**往外抛：调用方要据此把任务标失败、不能记成已发送。
            # 也不能落到下面的 Exception 分支被「发送失败」四个字糊过去——
            # 它跟「键盘流程抛异常」是两种完全不同的故障。
            log.error("发送未确认(%s): %s", name, e)
            raise
        except SendUnconfirmed as e:
            # 同上：必须让调用方看见，且不能写库（真实发出才落库）。
            log.error("发送无法确认(%s): %s", name, e)
            raise
        except SendAborted as e:
            # 没进会话就中止了：**确定没发出任何东西**，和上面两种性质不同
            # （可以放心重试，不存在重复打扰客户的问题）。抛给调用方落 failed，
            # 让面板上看得到真实原因，而不是被「发送失败」四个字糊过去。
            log.error("发送中止(%s): %s", name, e)
            raise
        except Exception as e:
            log.error("发送失败(%s): %s", name, e)

    def add_friend(self, account, verify_msg=""):
        """
        加好友。⚠️ PC 微信（尤其 Qt 版）普遍无法稳定发出好友申请，
        键盘流程不可靠，这里只提示并建议回手机端，不强行模拟。
        """
        log.warning("PC 微信加好友受限：Qt 版微信没有可靠的键盘流程可发出好友申请。")
        if self.dry_run:
            log.info("[DRY-RUN] 若走手机端，预期动作：搜索 %s → 申请添加 → 验证语 %r",
                     account, verify_msg)
            return
        log.warning("未执行模拟加好友（避免误操作和风控）。请改用手机端微信发送好友申请；"
                    "或如确需 PC 实现，需基于图像识别点击「添加」按钮，另开方案。")

    def send_template(self, template_key, tag=None, execute=None):
        tmpl = self.cfg["templates"].get(template_key)
        if not tmpl:
            log.error("模板不存在: %s", template_key)
            return
        if execute is None:
            execute = not self.dry_run
        targets = [
            c for c in self.cfg.get("contacts", [])
            if (tag is None or tag in c.get("tags", []))
        ]
        log.info("模板[%s] 命中 %d 人（execute=%s）", template_key, len(targets), execute)
        for c in targets:
            name = c.get("name", "")
            text = tmpl.replace("{name}", name)
            if execute:
                self.send_to_contact(name, text)
            else:
                log.info("[DRY-RUN] 计划发送给 %s : %s", name, text)

    def send_ai(self, intent, tag=None):
        """用大模型按 intent（沟通意图）为每位命中客户生成个性化文案并发送。
        文案生成走 ai_client；发送复用键盘流程。dry_run 时只生成+打印，不真发。"""
        targets = [
            c for c in self.cfg.get("contacts", [])
            if (tag is None or tag in c.get("tags", []))
        ]
        log.info("AI 外发 命中 %d 人（intent=%r, dry_run=%s）", len(targets), intent, self.dry_run)
        if not intent:
            log.error("AI 外发需要 --intent（沟通意图），例如 '给VIP客户做个周末问候'")
            return
        for c in targets:
            name = c.get("name", "")
            try:
                text = ai_generate(self.cfg, c, intent)
            except Exception as e:
                log.error("AI 生成失败(%s): %s", name, e)
                continue
            log.info("[AI 文案→%s] %s", name, text)
            self.send_to_contact(name, text)  # 内部按 dry_run 决定是否真发

    def inspect(self):
        """列出当前被识别为个人微信的窗口（用 ctypes，不需要控件树）。"""
        wins = find_wechat_windows()
        if not wins:
            log.info("未找到个人微信主窗口（已排除企业微信和浏览器页面）。"
                     "请确认个人微信已打开并登录。")
            return
        log.info("找到 %d 个个人微信窗口：", len(wins))
        for hwnd, title in wins:
            log.info("  hwnd=%s title=%r class=%r", hwnd, title, _class_of(hwnd))
        log.info("键盘驱动不需要控件树，--inspect 仅用于确认窗口可被定位（只认个人微信）。")

    def run_schedule(self):
        sched = BlockingScheduler()
        for t in self.cfg.get("tasks", []):
            if not t.get("enabled", False):
                continue
            tag = t.get("tag")
            if t.get("ai"):
                intent = t.get("intent", "")
                sched.add_job(
                    lambda intent=intent, tag=tag: self.send_ai(intent, tag),
                    CronTrigger.from_crontab(t["cron"]),
                    id=t["id"],
                )
                log.info("已注册定时任务(AI) %s (%s) intent=%r tag=%s",
                         t["id"], t["cron"], intent, tag)
            else:
                tk = t["template"]
                sched.add_job(
                    lambda tk=tk, tag=tag: self.send_template(tk, tag),
                    CronTrigger.from_crontab(t["cron"]),
                    id=t["id"],
                )
                log.info("已注册定时任务 %s (%s) template=%s tag=%s",
                         t["id"], t["cron"], tk, tag)
        log.info("调度器启动…(Ctrl+C 退出)")
        sched.start()


def main():
    ap = argparse.ArgumentParser(description="个人微信 PC 端键盘驱动自动化骨架（非注入）")
    ap.add_argument("--send", action="store_true", help="一次性发送（模板或 AI）到标签联系人")
    ap.add_argument("--template")
    ap.add_argument("--ai", action="store_true", help="用 AI 按 intent 生成外发文案（需 --intent）")
    ap.add_argument("--intent", default="", help="AI 沟通意图，如 '给VIP客户做个周末问候'")
    ap.add_argument("--tag", help="只发给带该标签的联系人；省略=全部")
    ap.add_argument("--add-friend", help="对方微信号/手机号（PC 加好友受限，仅提示）")
    ap.add_argument("--verify-msg", default="", help="好友验证语")
    ap.add_argument("--inspect", action="store_true", help="列出微信窗口，确认可被定位")
    ap.add_argument("--schedule", action="store_true", help="运行定时调度（阻塞）")
    ap.add_argument("--no-dry", action="store_true", help="关闭 dry_run，真正发键")
    args = ap.parse_args()

    cfg = load_config()
    if args.no_dry:
        cfg["dry_run"] = False
        # 真发前再确认窗口存在
        try:
            focus_wechat()
        except RuntimeError as e:
            log.error(str(e))
            return
    bot = WeChatPCBot(cfg)

    if args.inspect:
        bot.inspect()
    elif args.add_friend:
        bot.add_friend(args.add_friend, args.verify_msg)
    elif args.send and args.ai:
        bot.send_ai(args.intent, args.tag)
    elif args.send and args.template:
        bot.send_template(args.template, args.tag)
    elif args.schedule:
        if args.no_dry:
            log.warning("定时任务将以真实模式运行（--no-dry）。确认 config 中任务已启用。")
        bot.run_schedule()
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
