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

# 转发窗里逐人勾选的节奏：库的 select() 一个人接一个人猛点、中间没停顿，
# Qt 界面容易反应不过来卡住。这里每人之间随机歇一下（秒），要调节奏改这两数。
SELECT_GAP_MIN = 0.4
SELECT_GAP_MAX = 0.9


class ForwardError(RuntimeError):
    """转发前置条件不满足（依赖/UI/来源消息）。"""


# ---------------------------------------------------------------------------
# 转发窗 UIA 树的遍历策略（2026-11-24 实测定位「第三人开始卡死」根因）
#
# 事实（本机多次实测）：
#   干净窗口 52 节点、整树遍历 0.01s，候选行在 depth 4~8；
#   选过人之后，微信 Qt 的 accessibility 树在 depth 13+ 开始**指数膨胀**
#   （depth 12→16 的节点数是 27→54→27→81→162，约每层 ×3），
#   到 depth 30 能到 5 万+节点，整树遍历 17~18 秒。
#   选人时反复整树遍历（旧 _visible_rows / 库 _rows / 库 _find / 库 send）
#   等于每选一个人烧掉好几个 18 秒 —— 第 3、4 个人必卡，与选谁无关。
#   纯遍历不点不搜不会膨胀，是「选人动作 + 深遍历」叠加放大。
#
# 对策：候选行/已选行/搜索框都在浅层，只做 BFS 浅层遍历 + 节点预算，
# 绝不进入膨胀区。所有需要「整树找控件」的地方都换成 _walk/_find_limited。
# ---------------------------------------------------------------------------
WALK_MAX_DEPTH = 14          # 候选行实测在 4~8，留足余量
WALK_MAX_NODES = 3000        # 节点预算：限深 14 时正常仅 200 上下；放大到 3000
                             # 是为了「已选栏 9 行 + 膨胀起始层」也读全，最坏 0.9s


def _walk(root, max_depth=WALK_MAX_DEPTH, max_nodes=WALK_MAX_NODES):
    """层序（BFS）遍历转发窗子树，只走到浅层且受节点预算约束。

    返回 [(depth, ctrl)]。用 BFS 而非 DFS：DFS 会一头扎进 depth 13+ 的膨胀
    分支（几万节点），BFS 先把浅层的行扫完就触发预算停止。
    """
    if root is None:
        return []
    from collections import deque
    out = []
    q = deque([(root, 0)])
    n = 0
    while q and n < max_nodes:
        el, d = q.popleft()
        if d > max_depth:
            continue
        out.append((d, el))
        n += 1
        try:
            kids = el.GetChildren()
        except Exception:
            continue
        for k in kids:
            q.append((k, d + 1))
    return out


def _find_limited(win, name=None, cls=None, aid=None,
                  max_depth=None, max_nodes=None):
    """在浅层子树里找第一个匹配控件（替代库 _find / _find_by_aid 的深遍历）。"""
    if win is None or (name is None and cls is None and aid is None):
        return None
    kw = {}
    if max_depth is not None:
        kw["max_depth"] = max_depth
    if max_nodes is not None:
        kw["max_nodes"] = max_nodes
    for _d, el in _walk(win, **kw):
        try:
            if aid is not None:
                if (getattr(el, "AutomationId", "") or "") == aid:
                    return el
                continue
            if cls is not None and cls not in (el.ClassName or ""):
                continue
            if name is not None and (el.Name or "").strip() != name:
                continue
            return el
        except Exception:
            continue
    return None


def _selected_fast(ops, win=None, max_nodes=None, max_depth=None):
    """右侧已选栏里的收件人（Name=「移除<显示名>」），自己走浅层。

    不用库的 ops.selected()——它内部 _rows 是 max_depth=30 的整树 DFS，
    选过人后会撞进膨胀区（一次 18 秒）。
    """
    from wechatauto.forward import CHOSEN_ROW_CLS
    if win is None:
        _h, win = ops._forward_window()
    if win is None:
        return []
    kw = {}
    if max_nodes is not None:
        kw["max_nodes"] = max_nodes
    if max_depth is not None:
        kw["max_depth"] = max_depth
    out = []
    for _d, el in _walk(win, **kw):
        try:
            cn = el.ClassName or ""
            nm = (el.Name or "").strip()
        except Exception:
            continue
        if CHOSEN_ROW_CLS not in cn:
            continue
        if nm.startswith("移除"):
            who = nm[len("移除"):].strip()
            if who and who not in out:
                out.append(who)
    return out


def _clear_forward_search(ops, force=False):
    """清空转发窗的搜索框——**只在确实搜过时清**（对齐库 labels._clear_picker_search）。

    搜索词只会把行藏起来、不会凭空多出行，所以上一个人是直接翻列表点中的
    （没用搜索框）时，搜索框本来就是空的，根本不用清。旧代码每人都强制清一次，
    每次要 2~3 次整棵 UIA 树遍历（大列表实测 1.2~9.4s/人）——纯白花的时间。
    force=True 给调用方在明确知道要清时使用。
    """
    if not force and not getattr(ops, "_searched", None):
        return
    from wechatauto.labels import PICKER_CLEAR, SEARCH_BOX_NAME
    ops._searched = None
    h, win = ops._forward_window()
    if win is None:
        return
    btn = _find_limited(win, name=PICKER_CLEAR)
    if btn is not None:
        ops._click(h, btn, raise_it=False)
        return
    box = _find_limited(win, name=SEARCH_BOX_NAME)
    if box is not None:
        ops.uia._set_text(box, "")
        time.sleep(0.5)


def _visible_rows(ops, win=None):
    """浅层 BFS 一次收集转发窗候选行 → [(显示名, 行控件)]。

    候选行实测在 depth 4~8；用 _walk 只走浅层、不进 depth 13+ 的膨胀区，
    单次 0.01~0.05s（旧版整树 DFS 在选过人后要 18s）。
    """
    from wechatauto.labels import PICKER_ROW_CLS, PICKER_SEARCH_ROW_CLS
    if win is None:
        _h, win = ops._forward_window()
    if win is None:
        return []
    out, seen = [], set()
    for _d, el in _walk(win):
        try:
            cn = el.ClassName or ""
            nm = (el.Name or "").strip()
        except Exception:
            continue
        if not (PICKER_ROW_CLS in cn or PICKER_SEARCH_ROW_CLS in cn):
            continue
        if nm and nm not in seen:
            seen.add(nm)
            out.append((nm, el))
    return out


def _select_one_fast(ops, name):
    """勾选一人：一次遍历 + Python **精确**匹配，替代库 select_one 的多次全树查找。

    库 select_one 每行都调 _find（整棵 UIA 树 DFS），全等失败还进 3s deadline
    循环、每轮 3 次整树遍历——转发窗列表大时一个人实测 40s。这里：
      * 可视区行一次遍历全读出，Python 精确匹配，命中就直接点；
      * 可视区没有才用搜索框过滤，之后每轮也只读一次；
      * 精确匹配为主；**搜索结果里「包含搜索词」的行恰好唯一时**才用它
        （同名/相近名会有多个候选，那就一个都不用，绝不猜、绝不误点）；
      * 搜索框写不进去 / 始终找不到 → **直接如实失败**，绝不退回库的深遍历
        （退回库 = 每次 18s 的整树 DFS，正是「搜出来了却卡住不点」的根因）。
    返回同库 select_one 的 dict，另带 path（visible/search）便于耗时日志。
    """
    from wechatauto.labels import _result, SEARCH_BOX_NAME
    name = (name or "").strip()
    if not name:
        return _result(False, "bad-name：没给收件人", path="none")

    h, win = ops._forward_window()
    if win is None:
        return _result(False, "no-picker：转发窗不在", path="none")
    if name in _selected_fast(ops, win):
        return _result(True, "already", who=name, path="visible")

    # 上一次真搜过才先清词（守卫在 _clear_forward_search 里），否则列表没被过滤
    if getattr(ops, "_searched", None):
        _clear_forward_search(ops, force=True)
        h, win = ops._forward_window()

    rows = _visible_rows(ops, win)
    hit = next((row for nm, row in rows if nm == name), None)
    path = "visible"

    if hit is None:
        box = _find_limited(win, name=SEARCH_BOX_NAME)
        if box is None or not ops.uia._set_text(box, name):
            return _result(False, "no-search：搜索框写不进去", who=name, path="search")
        ops._searched = name
        path = "search"
        deadline = time.time() + 4.0
        while time.time() < deadline:
            _h, win2 = ops._forward_window()
            rows = _visible_rows(ops, win2 or win)
            hit = next((row for nm, row in rows if nm == name), None)
            if hit is not None:
                h = _h
                break
            # 精确没命中：搜索结果里「包含搜索词」的行**恰好唯一**才用
            # （高亮/空白差异会让 Name 不完全相等；多个候选则一律不用，防误点）
            cands = [r for nm, r in rows if name in nm or nm in name]
            if len(cands) == 1:
                hit = cands[0]
                h = _h
                break
            time.sleep(0.4)

    if hit is None:
        # 找不到就如实失败、跳到下一个人；绝不退回库深遍历（那会 18s/次卡死）
        return _result(False, "not-found：选择框里没搜到「%s」" % name,
                       who=name, path="search")

    r = ops._check_row(h, hit)
    if not r.get("ok"):
        r.setdefault("path", path)
        return r
    if name not in _selected_fast(ops):
        # 点了但没进右栏——不重试点（再点会取消勾选！），如实报失败
        return _result(False, "not-checked：点了但没进右侧已选栏", who=name, path=path)
    return _result(True, "ok", who=name, path=path)


def _select_paced(ops, names):
    """逐人勾选：每人之间留间隔、搜过才清搜索框，返回同 ops.select 的结构。

    选人用 :func:`_select_one_fast`（浅层 BFS + 精确匹配），绕开库 select_one
    在大列表下「每人多次整树遍历」的慢路径；找不到就如实记失败，不退回库。
    """
    from wechatauto.labels import _result
    picked, missing = [], []
    total = len(names)
    for idx, nm in enumerate(names):
        t0 = time.time()
        if idx:
            # 只有上一个人真搜过才清（否则搜索框本来就是空的，清一次纯浪费）
            tc = time.time()
            _clear_forward_search(ops)
            clear_ms = (time.time() - tc) * 1000
            gap = random.uniform(SELECT_GAP_MIN, SELECT_GAP_MAX)
            time.sleep(gap)
        else:
            clear_ms = 0.0
        ts = time.time()
        r = _select_one_fast(ops, nm)
        sel_ms = (time.time() - ts) * 1000
        ok = bool(r.get("ok"))
        print("[forward][select] %d/%d %s -> %s | %s 清空%.0fms 勾选%.0fms 本步%.1fs"
              % (idx + 1, total, nm[:20], "OK" if ok else "失败:%s" % r.get("reason"),
                 r.get("path", "?"), clear_ms, sel_ms, time.time() - t0), flush=True)
        (picked if ok else missing).append(
            nm if ok else {"name": nm, "reason": r.get("reason", "")})
    _h, win = ops._forward_window()
    return _result(True, "ok", picked=picked, missing=missing,
                   selected=_selected_fast(ops, win) if win is not None else [])


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
    t0 = time.time()
    uia = ops.uia
    ops.close_dialog()
    if chat:
        if not uia.open_chat(chat):
            return {"ok": False, "reason": "no-chat：打不开会话「%s」" % chat}
    elif not uia.ensure_window():
        return {"ok": False, "reason": "no-window：微信窗口没就绪"}
    t_open = time.time()

    hit = uia.find_in_message_list(
        lambda cn, nm: match in (nm or ""), match_last=True, max_scrolls=30)
    t_find = time.time()
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
    print("[forward][open] 开会话%.1fs 找卡片%.1fs 开窗共%.1fs (落点 %d,%d)"
          % (t_open - t0, t_find - t_open, time.time() - t0, x, y), flush=True)
    return {"ok": True, "hwnd": h, "win": win, "point": (x, y)}


def _fill_leave_message(ops, note):
    """在转发窗底部留言框填入文案（卡片 + 文案一起转发）。尽力而为，返回是否填上。"""
    from wechatauto.forward import LEAVE_INPUT_AID

    _h, win = ops._forward_window()
    if win is None:
        return False
    box = _find_limited(win, aid=LEAVE_INPUT_AID)
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


def _send_fast(ops, expect):
    """点「发送」：自己校验人数 + 点按钮，绕开库 send() 的整树遍历。

    库 ops.send() 里 selected()/_find_by_aid() 都是 max_depth=30 的深遍历，
    选过人后单次要 18s，这里换成浅层的 _selected_fast/_find_limited。
    两个人的读数（按钮上的「分别发送(N)」和右侧已选栏人头）仍必须和预期一致，
    否则不点——这一步点下去就是真发，没有后悔药。
    """
    from wechatauto.forward import AID_CONFIRM, parse_send_button
    h, win = ops._forward_window()
    if win is None:
        return {"ok": False, "reason": "no-picker：转发窗不在"}
    btn = _find_limited(win, aid=AID_CONFIRM)
    if btn is None:
        btn = _find_limited(win, aid=AID_CONFIRM, max_nodes=8000, max_depth=16)
    if btn is None:
        return {"ok": False, "reason": "no-send-btn：找不到发送按钮"}
    ok, n = parse_send_button(btn.Name or "")
    if not ok:
        return {"ok": False, "reason": "no-send-btn：按钮不可点（写 %r）" % n}
    if expect == 0:
        return {"ok": False, "reason": "no-recipient：右侧一个人都没选，不点发送"}
    # 人数判据**以按钮上的「分别发送(N)」为准**——N 是微信自己按实际勾选算出来的，
    # 最可靠。右侧已选栏是虚拟列表，只实例化可视区几行（实测勾 9 人只读得到 4 行），
    # 拿它当「必须等于 N」会误拦成「选完了却不发送」。已选栏只用来防「比按钮还多」
    # 这种自相矛盾的数据。
    if n != expect:
        return {"ok": False,
                "reason": "count-mismatch：按钮写 %s 人 / 预期 %s 人，先不点"
                          % (n, expect),
                "on_button": n}
    chosen = _selected_fast(ops, win)
    if len(chosen) > n:
        return {"ok": False,
                "reason": "count-mismatch：已选栏 %s 人比按钮 %s 人多，先不点"
                          % (len(chosen), n),
                "on_button": n, "chosen": chosen}
    r = ops._click(h, btn)
    if not r.get("ok"):
        return r
    time.sleep(2.0)
    if ops._forward_window()[1] is not None:
        return {"ok": False, "reason": "still-open：点了发送但窗口没关（没发出去？）"}
    return {"ok": True, "on_button": n, "chosen": chosen}


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
        sel = _select_paced(ops, names)
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
                rs = _send_fast(ops, len(picked))
                if not rs["ok"]:
                    # 一定要打出来：否则「选完了却没发送」只能看到取消了窗口，
                    # 看不出是人数对不上还是找不到按钮
                    print("[forward][send] 本批 %d 人未发送：%s"
                          % (len(picked), rs.get("reason")), flush=True)
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
                    time.sleep(random.uniform(10.0, 15.0))
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
