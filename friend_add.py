#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
friend_add.py —— 批量加好友的**生产实现**（单次加一个人）

流程（全程在 UI 门内、拟人光标轨迹）：
  1. 微信抬到前台 → 回聊天页 → 点搜索框 → 输入 keyword；
  2. 若结果里出现「联系人」分区 → 已经是好友，直接返回 already（不重复申请）；
  3. 点下拉里的「网络查找手机/QQ号」→ 结果页点「添加到通讯录」；
  4. 点「填入」把常用申请语灌进输入框（失败就中止，绝不发默认的
     「我是中照金智-XX」）；
  5. 点「确定」提交申请。备注框全程不碰。

说明：
- 与 wx_search_test.py 重复了一套定位原语（_scan_wechat / _click_fill /
  _click_confirm）。**故意不复用**：测试脚本是诊断工具，随时会改输出和流程，
  生产链路不能跟着它晃动。要合并的话应该抽第三个模块，不是互相 import。
- 这里只做「加一个人」这一件事，节奏/上限/重试都在 auto_reply 的
  AddFriendWorker 里控制。单独跑本模块的 CLI 只用于验证或手工补加。

用法：
    python friend_add.py --import 名单.csv      # 导入名单（CSV）
    python friend_add.py --one 13800138000      # 立刻加一个（调试用，不受限速）
    python friend_add.py --list                 # 看队列与统计
"""
import argparse
import csv
import ctypes
import io
import sys
import time

import ui_gate


# --------------------------------------------------------------------------
# 微信窗口 / 控件定位（只扫微信进程，不扫桌面树）
# --------------------------------------------------------------------------
_WECHAT_EXES = ("weixin.exe", "wechatappex.exe")
_k32 = ctypes.windll.kernel32


def _pid_image_name(pid):
    try:
        h = _k32.OpenProcess(0x1000, False, int(pid))
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(320)
            n = ctypes.c_ulong(320)
            if _k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
                return buf.value.replace("/", "\\").rsplit("\\", 1)[-1].lower()
        finally:
            _k32.CloseHandle(h)
    except Exception:
        pass
    return ""


def _wechat_windows():
    import uiautomation as auto
    try:
        tops = auto.GetRootControl().GetChildren()
    except Exception:
        return []
    out = []
    for c in tops:
        try:
            pid = int(c.ProcessId)
        except Exception:
            continue
        if pid and _pid_image_name(pid) in _WECHAT_EXES:
            out.append(c)
    return out


def _point_belongs_to_foreground(rect):
    """落点最上层窗口是不是当前前台窗口（防按坐标盲投到被压住的别的窗口）。"""
    try:
        from wechatauto.guia import window_owner_pid
        u = ctypes.windll.user32
        fg = u.GetForegroundWindow()
        if not fg:
            return False
        pid = ctypes.c_ulong()
        u.GetWindowThreadProcessId(fg, ctypes.byref(pid))
        owner = window_owner_pid((int(rect[0]) + int(rect[2])) // 2,
                                 (int(rect[1]) + int(rect[3])) // 2)
        return owner is None or owner == pid.value
    except Exception:
        return True


def _scan_all(root, needles, type_kw=None, cap=6000, max_depth=32):
    """子树里收集 name 命中 needles 的控件 [(name, type, rect, ctrl)]（只读）。"""
    if root is None:
        return []
    hits, stack, seen = [], [(root, 0)], 0
    while stack and seen < cap:
        el, dep = stack.pop()
        seen += 1
        if dep > max_depth:
            continue
        try:
            nm = (el.Name or "").strip()
        except Exception:
            nm = ""
        if nm:
            try:
                ct = el.ControlTypeName or ""
            except Exception:
                ct = ""
            if (not type_kw or type_kw in ct) and any(nd in nm for nd in needles):
                rect = None
                try:
                    r = el.BoundingRectangle
                    if r and r.width() > 0 and r.height() > 0:
                        rect = (r.left, r.top, r.right, r.bottom)
                except Exception:
                    rect = None
                hits.append((nm, ct, rect, el))
        try:
            for k in el.GetChildren():
                stack.append((k, dep + 1))
        except Exception:
            pass
    return hits


def _scan_wechat(main, needles, type_kw=None):
    """只在微信窗口里找：先主窗，没有再扫其它微信顶层窗口。"""
    hits = _scan_all(main, needles, type_kw=type_kw)
    if hits:
        return hits
    try:
        main_hwnd = int(main.NativeWindowHandle)
    except Exception:
        main_hwnd = 0
    seen = {main_hwnd} if main_hwnd else set()
    for c in _wechat_windows():
        try:
            h = int(c.NativeWindowHandle)
        except Exception:
            h = 0
        if h and h in seen:
            continue
        hits.extend(_scan_all(c, needles, type_kw=type_kw))
        if h:
            seen.add(h)
    return hits


def _rect_cy(rect):
    if not rect:
        return None
    return (int(rect[1]) + int(rect[3])) / 2.0


def _click_fill(uia, win):
    """点「填入」（常用申请语）。只认名字恰为「填入」的控件，找不到不猜。"""
    hits = _scan_wechat(win, ["填入"])
    target = next((h for h in hits if h[0].strip() == "填入"), None)
    if target is None:
        target = next((h for h in hits
                       if h[0].strip().endswith("填入") and len(h[0].strip()) <= 4),
                      None)
    if target is None:
        names = " | ".join(sorted({h[0][:40] for h in hits})) or "（无）"
        return False, "没找到独立的「填入」控件（找到的是：%s）" % names
    nm, _ct, rect, el = target
    if rect and not _point_belongs_to_foreground(rect):
        return False, "「填入」落点不属于当前前台窗口"
    ok = uia._click_ctrl(el)
    return ok, ("已点「填入」" if ok else "点「填入」失败（控件矩形不可用）")


def _click_confirm(uia, win):
    """点申请对话框的「确定」：用同一行的「取消」当锚点，配不上就不点。"""
    oks = _scan_wechat(win, ["确定"], type_kw="Button")
    if not oks:
        return False, "没找到「确定」按钮"
    cancels = _scan_wechat(win, ["取消"], type_kw="Button")
    crow = _rect_cy(cancels[0][2]) if cancels else None
    target = None
    if crow is not None:
        def _dy(h):
            cy = _rect_cy(h[2])
            return abs(cy - crow) if cy is not None else 1e9
        best = min(oks, key=_dy)
        if _dy(best) <= 60:
            target = best
    if target is None:
        return False, ("微信里有 %d 个「确定」，无法确认哪个属于申请对话框"
                       "（没配对到同一行的「取消」）" % len(oks))
    nm, _ct, rect, el = target
    if rect and not _point_belongs_to_foreground(rect):
        return False, "「确定」落点不属于当前前台窗口"
    ok = uia._click_ctrl(el)
    return ok, ("已点「确定」" if ok else "点「确定」失败（控件矩形不可用）")


def _list_children_of(uia):
    """搜索下拉的全部子项（含被 _collect_results 过滤掉的建议项）。"""
    lst = uia._find_search_list()
    if lst is None:
        return []
    try:
        children = lst.GetChildren()
    except Exception:
        return []
    out = []
    for c in children:
        try:
            name = (c.Name or "").strip()
        except Exception:
            name = ""
        try:
            aid = c.AutomationId or ""
        except Exception:
            aid = ""
        if not name and not aid:
            continue
        out.append({"cell": c, "name": name,
                    "aid": aid})
    return out


def _esc():
    try:
        import pyautogui
        pyautogui.press("esc")
    except Exception:
        pass


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def add_friend(keyword, settle=1.6, need_confirm=True, ui_timeout=None):
    """加一个人。返回 {'status': ..., 'reason': ...}。

    status:
      sent      申请已提交
      already   对方已在通讯录（不重复申请）
      failed    技术执行失败（原因在 reason）
    注意：**不等待对方通过**——发完即走，通过由收消息线程异步检测。
    """
    keyword = (keyword or "").strip()
    if not keyword:
        return {"status": "failed", "reason": "空关键词"}

    from wechatauto import rhythm
    from wechatauto.uia_driver import WeChatUIA
    import pyautogui

    import wechat_ui

    try:
        with ui_gate.hold("加好友 %s" % keyword, timeout=ui_timeout):
            try:
                wechat_ui.focus_wechat()
            except Exception as e:      # noqa: BLE001
                return {"status": "failed", "reason": "无法把微信抬到前台: %s" % e}

            uia = WeChatUIA()
            if not uia.ensure_window():
                return {"status": "failed", "reason": "找不到微信主窗口/UIA 树未物化"}
            uia.back_to_chat_tab()
            win = uia._win

            # 1) 拟人轨迹点搜索框
            rect = uia.search_box_rect()
            if rect and _point_belongs_to_foreground(rect):
                x, y = rhythm.point(rect)
                rhythm.move_to(ctypes.windll.user32, x, y)
                rhythm.nap(0.15)
                pyautogui.click()
                rhythm.nap(0.4)

            box = uia._search_box_present(win) or uia._search_box(win, expand=True)
            if box is None:
                return {"status": "failed", "reason": "搜索框不在控件树里"}
            if not uia._set_text(box, keyword):
                uia._paste_into(box, keyword, clear=True)
            rhythm.nap(settle)

            # 2) 已是好友？→ 不重复申请
            try:
                results = uia._collect_results(keyword, settle=settle) or []
            except Exception:
                results = []
            if any((r.get("section") or "") == "联系人" for r in results):
                _esc()
                return {"status": "already", "reason": "搜索结果在「联系人」分区"}

            # 3) 点「网络查找手机/QQ号」
            items = _list_children_of(uia)
            target = next((it for it in items if "网络查找" in it["name"]), None)
            if target is None:
                target = next((it for it in items if "查找手机" in it["name"]), None)
            if target is None:
                _esc()
                names = " | ".join(it["name"][:30] for it in items[:5]) or "（空）"
                return {"status": "failed",
                        "reason": "下拉里没有「网络查找」（现有：%s）" % names}
            uia._click_ctrl(target["cell"])
            rhythm.nap(1.4)

            # 4) 点「添加到通讯录」
            btns = (_scan_wechat(win, ["添加到通讯录"], type_kw="Button")
                    or _scan_wechat(win, ["添加到通讯录"]))
            if not btns:
                _esc()
                return {"status": "failed", "reason": "结果页没有「添加到通讯录」"}
            nm, _ct, brect, bel = btns[0]
            if brect and not _point_belongs_to_foreground(brect):
                _esc()
                return {"status": "failed", "reason": "「添加到通讯录」不在前台窗口"}
            uia._click_ctrl(bel)
            rhythm.nap(1.4)

            # 5) 点「填入」（失败就中止，绝不发默认申请语）
            rhythm.nap(0.3)
            fok, fmsg = _click_fill(uia, win)
            if not fok:
                _esc()
                return {"status": "failed", "reason": "填入失败: %s" % fmsg}
            rhythm.nap(0.6)

            # 6) 点「确定」
            if need_confirm:
                ok, msg = _click_confirm(uia, win)
                if not ok:
                    _esc()
                    return {"status": "failed", "reason": msg}
                rhythm.nap(1.2)

            _esc()
            rhythm.nap(0.3)
            return {"status": "sent", "reason": None}
    except ui_gate.UITimeoutError as e:
        return {"status": "failed", "reason": "UI 排队超时: %s" % e}
    except Exception as e:              # noqa: BLE001
        return {"status": "failed", "reason": "异常: %s" % e}


# --------------------------------------------------------------------------
# 名单导入 / CLI
# --------------------------------------------------------------------------
def import_text(text, retry_days=30):
    """从**文本**导入名单（CSV 内容，或每行一个关键词）。返回 (新增, 跳过列表)。

    两种输入都认：
      ① 带表头：`keyword,name,note,source`（也认中文表头 手机号/微信号/姓名/背景/来源）
      ② 不带表头：每行一个手机号/微信号，也允许 `keyword,name,note,source` 逗号分隔
    没有表头时**不能**用 DictReader——它会把第一行当表头吃掉，第一个号码就丢了。
    """
    import monitor_store

    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return 0, [("", "内容为空")]
    lines = [ln for ln in text.split("\n") if ln.strip()]
    first = lines[0] if lines else ""
    # 表头判定必须**同时**满足「有逗号」和「命中表头词」——否则像 `myname123`
    # 这种含 name 的微信号会被当成表头，第一行直接丢掉。
    has_header = ("," in first) and (
        "keyword" in first.lower() or "name" in first.lower()
        or "手机号" in first or "微信号" in first)

    items = []
    if has_header:
        for r in csv.DictReader(io.StringIO(text)):
            items.append((
                (r.get("keyword") or r.get("手机号") or r.get("微信号") or "").strip(),
                (r.get("name") or r.get("姓名") or "").strip() or None,
                (r.get("note") or r.get("背景") or "").strip() or None,
                (r.get("source") or r.get("来源") or "").strip() or None,
            ))
    else:
        for ln in lines:
            parts = [p.strip() for p in ln.split(",")]
            if not parts or not parts[0]:
                continue
            items.append((
                parts[0],
                parts[1] if len(parts) > 1 and parts[1] else None,
                parts[2] if len(parts) > 2 and parts[2] else None,
                parts[3] if len(parts) > 3 and parts[3] else None,
            ))

    added, skipped = 0, []
    for kw, name, note, source in items:
        if not kw:
            continue
        tid, why = monitor_store.add_friend_task(
            kw, name=name, note=note, source=source, retry_days=retry_days)
        if why:
            skipped.append((kw, why))
        else:
            added += 1
    return added, skipped


def import_csv(path, retry_days=30):
    """从文件导入（自动试 UTF-8-BOM / UTF-8 / GBK，Excel 另存常见后两种）。"""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        return 0, [("", "文件不存在: %s" % path)]
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return import_text(raw.decode(enc), retry_days=retry_days)
        except UnicodeDecodeError:
            continue
    return 0, [("", "编码无法识别（试过 utf-8-sig/utf-8/gbk）")]


def main():
    ap = argparse.ArgumentParser(description="批量加好友：导入名单 / 单次加一个 / 看队列")
    ap.add_argument("--import", dest="csv_path", help="导入名单 CSV（keyword,name,note,source）")
    ap.add_argument("--one", help="立刻加一个（调试用，不受每日上限与间隔限制）")
    ap.add_argument("--list", action="store_true", help="查看队列与统计")
    a = ap.parse_args()

    import monitor_store
    monitor_store.init()

    if a.csv_path:
        added, skipped = import_csv(a.csv_path)
        print("[friend] 导入完成：新增 %d 条" % added)
        for kw, why in skipped[:30]:
            print("    跳过 %s —— %s" % (kw, why))
        if len(skipped) > 30:
            print("    …另有 %d 条跳过" % (len(skipped) - 30))
        return 0

    if a.list:
        print("[friend] 统计: %s" % monitor_store.friend_counts())
        print("[friend] 今日已发: %d" % monitor_store.friend_sent_today())
        for t in monitor_store.get_friend_tasks(limit=20):
            print("    #%s [%s] %s %s %s"
                  % (t["id"], t["status"], t["keyword"],
                     t["name"] or "", t["reason"] or ""))
        return 0

    if not a.one:
        ap.print_help()
        return 1

    tid, why = monitor_store.add_friend_task(a.one)
    if tid is None:
        print("[friend] 未入队：%s" % why)
        return 1
    if why:
        print("[friend] 未入队：%s" % why)
        return 0
    res = add_friend(a.one)
    print("[friend] %s → %s %s" % (a.one, res["status"], res["reason"] or ""))
    if res["status"] == "sent":
        monitor_store.update_friend_task(tid, status="sent",
                                         sent_at=int(time.time()))
    elif res["status"] == "already":
        monitor_store.update_friend_task(tid, status="already",
                                         reason=res["reason"])
    else:
        monitor_store.update_friend_task(tid, status="failed",
                                         reason=res["reason"], tries=1)
    return 0 if res["status"] in ("sent", "already") else 2


if __name__ == "__main__":
    sys.exit(main())
