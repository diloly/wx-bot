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
import time
import ctypes
from ctypes import wintypes

import pyautogui
import pyperclip
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

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
# 微信主窗口的 Qt 类名（实测 Qt51514QWindowIcon；微信升级可能变，仅作优先信号，不当硬条件）
_WECHAT_CLASS_HINT = "Qt515"
VK_MENU = 0x12           # Alt 键，用于解除 Windows 前台锁
KEYEVENTF_KEYUP = 0x0002
SW_RESTORE = 9


def _class_of(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def find_wechat_windows():
    """返回 [(hwnd, title), ...]，只认个人微信主窗口。
    双保险防误判（历史事故：标题含「微信」的看板浏览器窗口被当成微信，
    快捷键 Ctrl+F/粘贴/Enter 全发进浏览器）：
    ① 类名含 Qt515（微信 Qt 主窗口）——优先信号；
    ② 或标题精确等于「微信」（主窗口标题就是它；「微信客户维护·看板」这类一律排除）；
    类名是浏览器/系统容器的一律先排除。多个命中时 Qt 类窗口排前。"""
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
        if ("微信" not in title) or ("企业" in title) or ("开发者" in title):
            return
        cls = _class_of(hwnd)
        if any(b in cls for b in _BROWSER_CLASSES):
            return                      # 看板/网页窗口，不是微信
        if (_WECHAT_CLASS_HINT in cls) or title == "微信":
            found.append((hwnd, title))

    ENUMPROC = ctypes.WINFUNCTYPE(None, wintypes.HWND, wintypes.LPARAM)
    _user32.EnumWindows(ENUMPROC(_enum), 0)
    # 微信 Qt 主窗口优先（多窗口命中时）
    found.sort(key=lambda t: 0 if _WECHAT_CLASS_HINT in _class_of(t[0]) else 1)
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


def focus_wechat():
    """把个人微信主窗口激活到前台并返回 (hwnd, title)。找不到/激活失败则抛错。"""
    wins = find_wechat_windows()
    if not wins:
        raise RuntimeError(
            "找不到个人微信窗口（已自动排除企业微信/浏览器页面）。"
            "请确认个人微信已登录并打开；可用 --inspect 查看当前识别到的窗口。"
        )
    hwnd, title = wins[0]
    if not _activate(hwnd):
        fg = _user32.GetForegroundWindow()
        n = _user32.GetWindowTextLengthW(fg)
        buf = ctypes.create_unicode_buffer(n + 1)
        _user32.GetWindowTextW(fg, buf, n + 1)
        raise RuntimeError(
            "无法把微信窗口激活到前台（当前前台是 %r）。"
            "可能原因：微信刚启动未就绪 / 系统前台限制。"
            "请手动点一下微信窗口再重试。" % buf.value
        )
    return hwnd, title


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
    def send_to_contact(self, name, message, conv_id=None):
        """对一个联系人：搜索→进会话→发消息。execute 由 dry_run 控制。
        conv_id 传入微信 wxid 时，看板消息库按 wxid 归并会话（与收消息桥一致）；
        省略时按 name 归并（手动 CLI 发送的兼容行为）。"""
        plan = (
            f"聚焦微信窗口 → Ctrl+F 搜索 → 粘贴{name!r} → Enter 进会话 "
            f"→ 粘贴消息 → {_SEND_KEY} 发送 → 等待 {self.delay}s"
        )
        if self.dry_run:
            log.info("[DRY-RUN] 计划发送给 %s : %s", name, message)
            log.info("[DRY-RUN] 按键序列: %s", plan)
            return
        try:
            hwnd, title = focus_wechat()
            _require_foreground(hwnd)       # 关键：确保按键只发到微信，发错就中止
            self._hotkey("ctrl", "f")       # 打开搜索
            self._hotkey("ctrl", "a")       # 全选搜索框旧内容
            self._paste(name)               # 粘贴要搜的名字
            self._press("enter")            # 打开顶部搜索结果（需名字唯一）
            time.sleep(0.6)
            self._paste(message)            # 粘贴消息到会话输入框
            self._send_keystroke()          # 发送
            log.info("已发送消息给 %s", name)
            # 写回统一消息库，供网页看板即时上屏（失败不影响已发送）
            try:
                store_add_message(conv_id or name, "me", message, name=name)
                # 同时记 sent_log：收消息轮询再从库里读到这条自己的消息时跳过，避免双渲染
                store_add_sent(conv_id or name, message)
            except Exception as e:
                log.warning("写入看板消息库失败（不影响已发送）: %s", e)
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
