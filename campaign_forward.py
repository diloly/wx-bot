#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""campaign_forward.py —— 转发型活动：把微信里已有的一条消息卡片转发给名单。

为什么单独一条链路（2026-11-23）：
    服务号文章等「图文卡片」是微信里的结构化消息（appmsg），没法用「粘贴文字/
    图片 + 回车」凭空造出来——把链接贴进输入框只会得到纯文本。唯一可靠的发送
    方式是**右键那条已有消息 → 转发**。

    直接调 wechatauto 的 ``WeChat.ForwardMessage`` 有三个实测坑，本模块绕开：
      1. 它右键用裸 ``_right_click``，微信渲染子窗带 WS_EX_TRANSPARENT，点击被
         命中测试跳过 → 菜单不弹（``no-forward-item``）。这里用 ``_click_at``
         先摘透明再右键。注意：不能在 open_chat 和定位之间插
         ``wechat_ui.focus_wechat()``——它的 Alt tap 会打断 UIA 控件树，导致
         定位不到卡片、光标不动（2026-11-23 实测）。
      2. 它找「转发」菜单只扫主窗树 ``mmui::XMenuView``、depth≤6，菜单弹在独立
         顶层窗（Qt SaveBits 弹层）就找不到。这里主窗 + 顶层弹层都查。
      3. 它声明了 ``leave_message`` 参数但**函数体从没实现**。这里自己填留言框，
         实现「卡片 + 文案」一起转发。

    收件人用显示名（get_display_name），符合「搜索词只能是显示名」红线；结果按
    显示名映射回 conv_id，逐条回写。来源消息用 source_chat + source_match 定位，
    定位不到整条失败，不猜人、不乱转发。

用法::
    result = campaign_forward.send_forward_activity(
        targets, source_chat, source_match, note="附言文案")
    # targets: [{conv_id, name, kind}]
    # result:  {conv_id: (ok:bool, reason:str)} 逐人结果
"""
import random
import time


class ForwardError(RuntimeError):
    """转发前置条件不满足（依赖/UI/来源消息）。"""


def _display_to_conv(targets):
    """{显示名: conv_id}，同时兼容 targets 自带的 name。重名时**不猜**，记到冲突里。"""
    import monitor_store
    name2conv = {}
    conflicts = {}
    for t in targets or []:
        cid = (t.get("conv_id") or "").strip()
        if not cid:
            continue
        nm = (monitor_store.get_display_name(cid)
              or (t.get("name") or "").strip() or cid).strip()
        if not nm:
            continue
        if nm in name2conv and name2conv[nm] != cid:
            conflicts[nm] = True       # 两个不同人同名 → 转发窗无法区分，报出来
        name2conv[nm] = cid
    for nm in conflicts:
        name2conv.pop(nm, None)        # 冲突名从映射里剔掉，这些人标记失败
    return name2conv, conflicts


def _find_forward_menu(ops, want="转发"):
    """在右键菜单里找「转发...」，主窗树 + 可见顶层弹层窗都扫。

    wechatauto 原版只扫主窗 ``mmui::XMenuView`` depth≤6；微信 4.x 的右键菜单
    有时物化在独立顶层窗（Qt SaveBits 弹层）里，这里补上。只读遍历，不点不按。
    """
    import win32gui
    import uiautomation as auto
    from wechatauto import uia_driver

    uia = ops.uia

    def walk(root, want_cls, max_depth=14, allow_any=False):
        found = [None]

        def rec(c, d=0):
            if found[0] is not None or d > max_depth:
                return
            try:
                kids = c.GetChildren()
            except Exception:
                return
            for ch in kids:
                try:
                    nm = (ch.Name or "").strip()
                    cn = ch.ClassName or ""
                except Exception:
                    continue
                cls_ok = ("Menu" in cn or "XMenuView" in cn) if want_cls else True
                if (want in nm and cls_ok) or (allow_any and nm.startswith(want)):
                    found[0] = ch
                    return
                rec(ch, d + 1)

        rec(root)
        return found[0]

    roots = []
    if uia._win is not None:
        roots.append(uia._win)
    try:
        tops = []

        def cb(hwnd, _):
            if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd):
                cls = win32gui.GetClassName(hwnd)
                if any(k in cls for k in ("SaveBits", "Menu", "Popup")):
                    tops.append(hwnd)

        win32gui.EnumWindows(cb, None)
        for h in tops:
            try:
                ctrl = auto.ControlFromHandle(h)
            except Exception:
                continue
            if ctrl is not None:
                roots.append(ctrl)
    except Exception:
        pass

    # 第一轮：只认菜单类控件，避免误命中消息正文里的「转发」字样
    for root in roots:
        hit = walk(root, want_cls=True)
        if hit is not None:
            return hit
    # 第二轮：放宽到 Name 以「转发」开头的控件（菜单 class 变体兜底）
    for root in roots:
        hit = walk(root, want_cls=False, allow_any=True, max_depth=20)
        if hit is not None:
            return hit
    return None


def _bubble_point(r):
    """算消息行内气泡的落点（自己发的在右/别人的在左），跨 DPI、无硬编码像素。

    UIA 给的 ChatBubbleItemView 矩形横跨整行宽度，几何中心落在列表空白区，
    右键那里不出菜单。这里对这一行截图、按「非背景像素」的列重心定位气泡——
    和 wechatauto 的 _right_click_latest_row 同一套判据，只是它没用在按文字
    定位的分支。y 取行高 35%~65% 的随机带，避开顶部头像行。
    """
    from PIL import ImageGrab
    import numpy as np
    from wechatauto import rhythm

    img = ImageGrab.grab(bbox=(r.left, r.top, r.right, r.bottom))
    arr = np.array(img.convert("RGB"))
    mx, mn = arr.max(axis=2), arr.min(axis=2)
    bg = (mx > 235) & ((mx - mn) < 22)     # 近白/浅灰的界面背景
    col = (~bg).sum(axis=0)
    w = len(col)
    # 只看行右侧 1/3：自己发的气泡+头像在这段，别人的消息这段是纯背景。
    # 用比例而非固定像素，跨 DPI/分辨率通用；也避开左侧可能被别的窗盖住的污染。
    lo = int(w * 0.66)
    seg = col[lo:]
    total = int(seg.sum())
    if total >= 20:
        cx = int(r.left + lo + sum(i * seg[i] for i in range(len(seg))) / total)
    else:
        cx = (r.left + r.right) // 2
    cy = int(rhythm.spread(r.top + r.height() * 0.35,
                           r.top + r.height() * 0.65))
    return cx, cy


def _open_dialog(ops, chat, match):
    """稳健打开「微信发送给」转发窗。返回 dict（ok/reason/hwnd/win/point）。

    顺序很关键：open_chat（库内部已 _activate 把微信置前）→ 定位卡片 → 右键。
    **绝不能**在 open_chat 和 find 之间插 wechat_ui.focus_wechat()：它靠 Alt tap
    抢前台，会让 Qt 进入菜单激活态、打断 UIA 控件树，导致定位不到卡片、光标不动
    （2026-11-23 实测）。右键用 _click_at 的透明穿透命中渲染层。
    """
    uia = ops.uia
    ops.close_dialog()
    if chat:
        if not uia.open_chat(chat):
            return {"ok": False, "reason": "no-chat：打不开会话「%s」" % chat}
    elif not uia.ensure_window():
        return {"ok": False, "reason": "no-window：微信窗口没就绪"}

    hit = uia.find_in_message_list(
        lambda cn, nm: match in (nm or ""), match_last=True, max_scrolls=30)
    if hit is None:
        return {"ok": False,
                "reason": "no-message：可视区里找不到包含「%s」的消息" % match}
    r = hit[2]
    x, y = _bubble_point(r)           # 落在气泡上，不是整行中心的空白区
    uia._click_at(x, y, right=True)   # 带 WS_EX_TRANSPARENT 穿透，命中渲染层
    time.sleep(1.0)

    item = _find_forward_menu(ops, "转发")
    if item is None:
        return {"ok": False,
                "reason": "no-forward-item：右键后没找到「转发...」菜单（微信可能没在前台）",
                "point": (x, y)}
    ir = item.BoundingRectangle
    uia._click_at(int((ir.left + ir.right) // 2),
                  int((ir.top + ir.bottom) // 2))
    h, win = ops._wait_window("微信发送给")
    if win is None:
        return {"ok": False, "reason": "no-picker：点了「转发...」但选择窗没出现"}
    return {"ok": True, "hwnd": h, "win": win, "point": (x, y)}


def _fill_leave_message(ops, note):
    """在转发窗底部留言框填入文案（卡片 + 文案一起转发）。尽力而为，返回是否填上。"""
    from wechatauto.forward import LEAVE_INPUT_AID

    _h, win = ops._forward_window()
    if win is None:
        return False
    box = ops._find_by_aid(win, LEAVE_INPUT_AID)
    if box is None:
        return False
    # 留言框不靠回车发送（发送靠点按钮），ValuePattern 写入即可；失败再退回粘贴。
    if ops.uia._set_text(box, note):
        return True
    try:
        ops.uia._paste_into(box, note, clear=True)
        return True
    except Exception:
        return False


def _latest_ts(ops, conv_id):
    """会话里最新一条消息的 create_time（水位基线）。

    返回 (有无历史, 最新ts)：读不到/没有历史 → (False, 0)，据此在回读时保守处理，
    避免 baseline=0 时任何旧消息都被当成「新送达」。
    """
    try:
        rows = ops.db.get_messages(conv_id, limit=5) or []
        if not rows:
            return False, 0
        return True, max(int(r.get("create_time") or 0) for r in rows)
    except Exception:
        return False, 0


def _landed_by_conv(ops, conv_id, name, baseline):
    """回读「这条转发是否真送达」——用 conv_id 直查 + 发送前水位基线。

    baseline = (有历史?, 最新create_time)。两处跨设备坑都避开：
      1. 库的 ops._landed(name, since) 拿**显示名**做 username_by_nickname 反查，
         别的设备备注不同/没有备注就查不到 → 明明发出去了却误报「回读未确认」。
      2. since=int(time.time())-5 依赖系统时钟与微信 create_time 对齐，跨设备/时区
         时钟有偏差就误判。改用发送前该会话的最新 create_time 做水位，发送后只要
         出现更大的 create_time 就算送达，完全不依赖系统时钟。
    发送前没读到历史（has=False）时，退回库的显示名反查兜底；都不行才判失败。
    """
    try:
        if conv_id:
            has, ts = baseline if isinstance(baseline, tuple) else (True, baseline)
            rows = ops.db.get_messages(conv_id, limit=5) or []
            if has:
                return any(int(r.get("create_time") or 0) > ts for r in rows)
            # 没历史基线：读到了新消息也算（但要求 create_time>0，避免空数据误判）
            return any(int(r.get("create_time") or 0) > 0 for r in rows)
    except Exception:
        pass
    return ops._landed(name, int(time.time()) - 5)


def send_forward_batch(names, source_chat, source_match, note="", dry_run=False,
                       conv_map=None):
    """转发**一批**（≤9 人）已有消息。返回 {显示名: (ok, reason)}。

    一批 = 一个转发窗：开窗→勾选本批→填留言→点发送→回读。
    **不持有 ui_gate、不加批间 sleep**——由调用方（worker）掌控门和节奏，
    这样每批之间能释放 UI 门给自动回复。调用方须已在 ui_gate.hold() 内。
    conv_map：{显示名: conv_id}，用于回读（显示名只用来在转发窗搜索/勾选）。
    """
    source_chat = (source_chat or "").strip()
    source_match = (source_match or "").strip()
    note = (note or "").strip()
    if not source_chat or not source_match:
        raise ForwardError("转发型活动缺来源会话或消息定位文字")
    names = [str(n).strip() for n in (names or []) if str(n).strip()]
    if not names:
        raise ForwardError("没有可转发的有效收件人")
    conv_map = conv_map or {}

    from wechatauto import WeChat
    wx = WeChat()
    ops = wx._forward()
    if ops is None:
        raise ForwardError("wechatauto UIA 驱动不可用，无法转发")

    sent, failed, skipped = [], [], []
    opened = _open_dialog(ops, source_chat, source_match)
    if not opened["ok"]:
        reason = opened["reason"]
        return {nm: (False, reason) for nm in names}
    try:
        sel = ops.select(names)
        picked = list(sel.get("picked") or [])
        skipped.extend(sel.get("missing") or [])
        if picked:
            if note:
                _fill_leave_message(ops, note)
            if not dry_run:
                # 发送前逐个采水位（本地库，不占 UI 门）；发送后只看有没有更新的消息。
                # 完全不依赖系统时钟，跨设备/时区一致。
                baseline = {}
                for who in picked:
                    cid = conv_map.get(who, "")
                    baseline[who] = _latest_ts(ops, cid) if cid else (False, 0)
                rs = ops.send(expect=len(picked))
                if not rs["ok"]:
                    failed.extend(picked)
                else:
                    time.sleep(1.5)
                    actual = rs.get("chosen") or picked
                    for who in actual:
                        landed = _landed_by_conv(
                            ops, conv_map.get(who, ""), who, baseline.get(who, 0))
                        (sent if landed else failed).append(who)
    finally:
        ops.close_dialog()

    if dry_run:
        return {"__dry_run__": (True, "预演完成，未发送（勾选 %d 人）" % len(picked))}
    sent_s, failed_s, skipped_s = set(sent), set(failed), set(skipped)
    results = {}
    for nm in names:
        if nm in sent_s:
            results[nm] = (True, "已转发")
        elif nm in failed_s:
            results[nm] = (False, "转发后回读未确认")
        elif nm in skipped_s:
            results[nm] = (False, "通讯录里找不到该收件人")
        else:
            results[nm] = (False, "未列入转发结果")
    return results


def send_forward_activity(targets, source_chat, source_match, note="", dry_run=False):
    """兼容入口：一次性跑完整批（整条活动独占 UI 门，顺序分批 + 批间 sleep）。

    新链路已改用「每批一个任务」走 auto_reply 的 _forward_q（可与自动回复穿插）。
    这里保留给独立脚本/手动调用；逐条结果按 conv_id 返回。
    """
    import ui_gate

    source_chat = (source_chat or "").strip()
    source_match = (source_match or "").strip()
    note = (note or "").strip()
    if not source_chat or not source_match:
        raise ForwardError("转发型活动缺来源会话或消息定位文字")

    name2conv, conflicts = _display_to_conv(targets)
    names = list(name2conv.keys())
    if not names:
        raise ForwardError("没有可转发的有效收件人（可能全是重名/无效）")

    chunk = 9
    all_results = {}
    with ui_gate.hold("转发活动消息给 %d 人" % len(names)):
        try:
            for i in range(0, len(names), chunk):
                group = names[i:i + chunk]
                all_results.update(
                    send_forward_batch(group, source_chat, source_match,
                                       note=note, dry_run=dry_run,
                                       conv_map=name2conv))
                if i + chunk < len(names) and not dry_run:
                    time.sleep(random.uniform(3.0, 5.0))
        except ForwardError:
            raise
        except Exception as e:              # noqa: BLE001
            raise ForwardError("转发异常：%s" % e)

    if dry_run:
        return {"__dry_run__": (True, "预演完成，未发送")}
    results = {}
    for name, cid in name2conv.items():
        ok, reason = all_results.get(name, (False, "未列入转发结果"))
        results[cid] = (ok, reason)
    import monitor_store
    for nm in conflicts:
        for t in targets or []:
            cid = (t.get("conv_id") or "").strip()
            if cid and (monitor_store.get_display_name(cid)
                        or (t.get("name") or "").strip()) == nm:
                results[cid] = (False, "收件人重名，无法区分，未转发")
    return results
