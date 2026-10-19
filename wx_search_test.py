#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wx_search_test.py —— 测「手机号 / 微信号 能不能搜到不在联系人里的用户」

做五件事：
  1) 拟人光标轨迹：把鼠标**平滑**移到微信搜索框并点击展开（rhythm.move_to，
     二次贝塞尔 + 末段减速，不是瞬移）；
  2) 把关键词打进搜索框（用 UIA SetValue，不动鼠标，避免抢焦点把词打进别处）；
  3) 读并打印搜索结果 + **下拉列表全部子项**；
  4) （可选 --click）点击某个结果项 —— 比如「网络查找手机/QQ号」；
  5) （可选 --add）再点结果页上的「添加到通讯录」，dump 弹出的
     「申请添加朋友」对话框，并**自动点「填入」**把常用申请语灌进输入框
     （不点「填入」发出去的就是默认的「我是中照金智-XX」）。
     **不碰备注框**；不想自动填入加 --no-fill；
  6) （可选 --confirm）再点对话框的「确定」，真正提交好友申请。
     默认**不点**，只 dump；要真发必须显式加 --confirm。

用法（用装 wechatauto 的那个 python）：
    python wx_search_test.py --search 13800138000
    python wx_search_test.py --search 13800138000 --click           # 点「网络查找」
    python wx_search_test.py --search 13800138000 --add             # 再点「添加到通讯录」
    python wx_search_test.py --search 13800138000 --add --confirm    # 再点「确定」发申请
    python wx_search_test.py --search abc_wechat_id --keep-open

说明：
- 全程在 UI 门（ui_gate）内执行，与 auto_reply.py / dashboard.py 的抢前台操作
  串行排队；运行时微信会被抢到前台几秒，别同时手动操作。
- 搜索框认「昵称 / 备注 / 微信号(alias) / 手机号」，**一般不认 wxid**（系统账号）。
- _collect_results 只收 aid 以 search_item_ 开头的项；「网络查找手机/QQ号」这类
  建议项 aid 不同，会被它过滤掉，所以本脚本自己遍历 ListControl 的孩子来展示/点击。
- **读控件只扫微信自己的窗口**（Weixin.exe / WeChatAppEx.exe 进程），不扫整棵
  桌面树——否则会把浏览器/IDE/任务栏的控件一起扫进来。
"""
import argparse
import ctypes
import sys

import ui_gate

# 点开「网络查找」结果项后，结果页上可能出现的控件名（只读、不点）
_RESULT_PAGE_HINTS = [
    "添加到通讯录", "加为好友", "申请添加", "添加朋友", "打招呼",
    "该用户不存在", "用户不存在", "搜索不到", "无法找到",
]

# 只认微信自己的进程：Weixin.exe 是外壳/主窗宿主，WeChatAppEx.exe 是内嵌
# webview 运行时。按进程筛窗口，才不会把 B 站/IDE/任务栏的控件扫进来。
_WECHAT_EXES = ("weixin.exe", "wechatappex.exe")

# 点「添加到通讯录」后弹出的「申请添加朋友」对话框里可能出现的控件名
_ADD_DIALOG_HINTS = [
    "发送添加朋友申请", "申请添加朋友", "添加朋友", "发送", "确定", "取消",
    "备注", "权限", "打招呼", "仅聊天", "朋友圈", "验证", "申请",
]

_k32 = ctypes.windll.kernel32


def _pid_image_name(pid):
    """进程 id → 可执行文件名（小写，如 weixin.exe）；查不到返回空串。"""
    try:
        h = _k32.OpenProcess(0x1000, False, int(pid))   # QUERY_LIMITED_INFORMATION
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


def _point_belongs_to_foreground(rect):
    """矩形中心点最上层的窗口，是否就是当前前台窗口。

    挡住「按坐标盲投点到被压住的别的窗口」——微信盖着还好，用户浏览器盖在
    微信上面时，mouse 事件会落到浏览器上（项目踩过同类事故）。
    """
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
        return True     # 判定不了就不拦（库自己的点击另带归属校验）


def _list_children_of(uia):
    """枚举搜索下拉列表的**全部**子项（含 _collect_results 会跳过的建议项）。

    _collect_results 只收 aid 以 search_item_ 开头的项；「网络查找手机/QQ号」
    这类建议项 aid 不同或为空，会被它直接 continue 掉。要看下拉里到底有什么、
    尤其要点击这一类建议项，只能自己遍历 ListControl 的孩子。
    """
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
        rect = None
        try:
            r = c.BoundingRectangle
            if r and r.width() > 0 and r.height() > 0:
                rect = (r.left, r.top, r.right, r.bottom)
        except Exception:
            rect = None
        out.append({"cell": c, "name": name, "aid": aid, "rect": rect})
    return out


def _match_item(items, needles):
    """按 needle 的**先后优先级**找项：先满足靠前的关键词（needles 空返回 None）。

    按关键词优先而不是按列表顺序，是为了让「网络查找」这类关键词稳赢——否则
    列表里靠前若有个名字带「QQ」的联系人，会先把它选中点开。
    """
    needles = [n for n in needles if n]
    if not needles:
        return None
    for nd in needles:
        for it in items:
            if nd in it["name"]:
                return it
    return None


def _scan_all(root, needles, type_kw=None, cap=6000, max_depth=32):
    """遍历子树，收集 name 命中 needles 的控件 [(name, type, rect, ctrl)]。

    type_kw 非空时只收 ControlTypeName 含该串的控件（如 "Button"）。只读，
    不点任何东西。cap/max_depth 限制遍历规模，避免在大树上卡住。
    """
    if root is None:
        return []
    hits = []
    stack = [(root, 0)]
    seen = 0
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


def _wechat_windows():
    """属于微信进程的顶层窗口控件（只微信，不含浏览器/IDE/任务栏）。"""
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


def _scan_wechat(main, needles, type_kw=None):
    """**只在微信窗口里**找控件：先扫主窗，没有再扫其它微信顶层窗口。

    绝不扫整棵桌面树——那会把别的应用的控件一起扫进来（实测输出里混进了
    B 站、IDE、任务栏的项，噪音压过真正要找的按钮）。
    """
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


def _win_title(ctrl):
    try:
        return (ctrl.Name or "").strip()
    except Exception:
        return ""


def _print_hits(hits, label):
    """去重打印命中控件（只展示，不点击）。"""
    if not hits:
        print("[test] %s：微信窗口里没读到相关控件"
              "（可能未渲染，或用 --keep-open 肉眼确认）。" % label)
        return
    print("[test] %s，微信窗口里读到的相关控件（仅展示，不点击）：" % label)
    seen = set()
    for nm, ct, _r, _c in hits:
        key = (nm, ct)
        if key in seen:
            continue
        seen.add(key)
        print("    - [%s] %s" % (ct, nm))


def _rect_cy(rect):
    """矩形竖直中心 y；矩形缺失返回 None。"""
    if not rect:
        return None
    return (int(rect[1]) + int(rect[3])) / 2.0


def _click_confirm(uia, win):
    """点「申请添加朋友」对话框的「确定」，返回 (是否点了, 说明)。

    安全第一：微信别处也可能有「确定」（设置页、其它弹窗）。所以
      ① 有「取消」时用它当锚点——取与「取消」**同一行**（y 中心最接近）的
         「确定」，这正是对话框底部那颗按钮；
      ② 配对不上就**不点**（宁可不发申请，也不点错别处的确定）。
    备注框的 Name 里也有字，但它不是 Button，不会被 type_kw="Button" 收进来。
    """
    oks = _scan_wechat(win, ["确定"], type_kw="Button")
    if not oks:
        return False, "微信窗口里没找到「确定」按钮"
    cancels = _scan_wechat(win, ["取消"], type_kw="Button")
    crow = _rect_cy(cancels[0][2]) if cancels else None
    target = None
    if crow is not None:
        def _dy(h):
            cy = _rect_cy(h[2])
            return abs(cy - crow) if cy is not None else 1e9
        best = min(oks, key=_dy)
        if _dy(best) <= 60:        # 同一行（按钮高度通常 30~45）
            target = best
    if target is None:
        return False, ("微信里有 %d 个「确定」，无法确认哪个属于申请对话框"
                       "（没配对到同一行的「取消」），未点击" % len(oks))
    nm, _ct, rect, el = target
    if rect and not _point_belongs_to_foreground(rect):
        return False, "「确定」落点不属于当前前台窗口，未点击"
    ok = uia._click_ctrl(el)
    return ok, ("已点击「确定」" if ok else "点击「确定」失败（控件矩形不可用？）")


def _edit_values(root, max_n=8):
    """收集子树里 EditControl 的文本值（尽力而为，读不到返回空列表）。

    用来确认「填入」有没有真的把常用申请语灌进输入框，以及顺带看到备注框
    有没有被动过。
    """
    out = []
    stack = [(root, 0)]
    seen = 0
    while stack and seen < 6000:
        el, dep = stack.pop()
        seen += 1
        if dep > 32:
            continue
        try:
            ct = el.ControlTypeName or ""
        except Exception:
            ct = ""
        if "Edit" in ct:
            val = ""
            try:
                vp = el.GetValuePattern()
                if vp is not None:
                    val = vp.Value or ""
            except Exception:
                val = ""
            if not val.strip():
                try:
                    val = el.Name or ""
                except Exception:
                    val = ""
            if val.strip():
                out.append(val.strip())
                if len(out) >= max_n:
                    break
        try:
            for k in el.GetChildren():
                stack.append((k, dep + 1))
        except Exception:
            pass
    return out


def _click_fill(uia, win):
    """点申请对话框里的「填入」链接，把常用申请语灌进输入框。返回 (ok, msg)。

    只认**名字就是「填入」**的控件：这句话所在的 TextControl 往往整行都是
    「常用申请语："……"填入」，点它的矩形会落在文字中间，所以宁可不点，
    也不按长文本控件的坐标猜——猜错会把光标点到句子中间，什么都不会发生。
    """
    hits = _scan_wechat(win, ["填入"])
    target = None
    for h in hits:
        if h[0].strip() == "填入":
            target = h
            break
    if target is None:
        for h in hits:
            nm = h[0].strip()
            if nm.endswith("填入") and len(nm) <= 4:
                target = h
                break
    if target is None:
        names = " | ".join(sorted({h[0][:40] for h in hits})) or "（无）"
        return False, ("没找到独立的「填入」控件（找到的是：%s），未点击"
                       % names)
    nm, _ct, rect, el = target
    if rect and not _point_belongs_to_foreground(rect):
        return False, "「填入」落点不属于当前前台窗口，未点击"
    ok = uia._click_ctrl(el)
    return ok, ("已点击「填入」" if ok else "点击「填入」失败（控件矩形不可用？）")


def run_search(keyword, keep_open=False, settle=1.6, click_needles=None,
               dump_all=False, do_add=False, do_confirm=False, do_fill=True):
    from wechatauto.uia_driver import WeChatUIA
    from wechatauto import rhythm
    import pyautogui

    import wechat_ui

    with ui_gate.hold("搜索测试 %s" % keyword):
        # 先把微信抬到前台：按坐标点击若落在被别的窗口压住的区域，会点到别的
        # 窗口上（项目踩过：滚轮/点击盲投进了压在上面的 IDE）。
        try:
            wechat_ui.focus_wechat()
        except Exception as e:
            print("[test] 无法把微信抬到前台，已中止（避免点击落到别的窗口）: %s" % e)
            return 2

        uia = WeChatUIA()
        if not uia.ensure_window():
            print("[test] 找不到微信主窗口 / UIA 树未物化（微信是否已登录并在运行？）")
            return 2
        if not uia.back_to_chat_tab():
            print("[test] 警告：未能确认回到聊天页 —— 搜索框可能不在控件树里，仍继续尝试")
        win = uia._win

        # ---- 1) 拟人光标轨迹：平滑移到搜索框 → 停一下 → 点击 ----
        rect = uia.search_box_rect()
        if rect and _point_belongs_to_foreground(rect):
            x, y = rhythm.point(rect)          # 框内随机点，避开正中
            rhythm.move_to(ctypes.windll.user32, x, y)   # 平滑走过去
            rhythm.nap(0.15)                   # 落点前停一下再按
            pyautogui.click()                  # 光标已在该点，只按下抬起
            print("[test] 拟人轨迹：光标平滑移到搜索框 (%d,%d) 并点击展开" % (x, y))
            rhythm.nap(0.4)
        elif rect:
            print("[test] 落点不属于当前前台窗口，跳过手动点击（改由库自己点击展开）")
        else:
            print("[test] 取不到搜索框坐标，跳过光标轨迹（改由库自己点击展开）")

        # 点击后搜索框应已出现在树里；兜底再让库点一次
        box = uia._search_box_present(win) or uia._search_box(win, expand=True)
        if box is None:
            print("[test] 搜索框不在控件树里，无法继续（微信版本/页面不对？）")
            return 3

        # ---- 2) 输入关键词（UIA SetValue，不动鼠标）----
        if not uia._set_text(box, keyword):
            uia._paste_into(box, keyword, clear=True)
        rhythm.nap(settle)

        # ---- 3) 读搜索结果 ----
        results = uia._collect_results(keyword, settle=settle) or []
        print("[test] 关键词 %r → _collect_results 命中 %d 条（仅 search_item_ 前缀）："
              % (keyword, len(results)))
        for r in results:
            print("    - [%s] %s" % (r.get("section") or "?", r.get("name")))
        if not results:
            print("    （无：该号码/微信号可能未注册微信，或搜索框不认这种输入）")

        # 下拉列表的全部子项（含被 _collect_results 过滤掉的网络查找项）
        items = _list_children_of(uia)
        if items or dump_all or click_needles:
            print("[test] 下拉列表全部子项 %d 个（含普通/建议项，aid 可能为空）："
                  % len(items))
            for it in items:
                tag = "" if it["name"] else "(无名)"
                print("    - aid=%-24s name=%s%s"
                      % (it["aid"] or "-", it["name"], tag))

        # ---- 4) 可选：点击某个结果项 ----
        if click_needles:
            target = _match_item(items, click_needles)
            if target is None:
                print("[test] 未找到名称含 %s 的项，不点击。"
                      % " / ".join(click_needles))
            elif target["rect"] and not _point_belongs_to_foreground(target["rect"]):
                print("[test] 目标项 %r 的落点不属于当前前台窗口，跳过点击。"
                      % target["name"])
            else:
                print("[test] 拟人点击结果项：%r（aid=%s）"
                      % (target["name"], target["aid"] or "-"))
                # _click_ctrl 内部走 _click_at → _set_cursor → rhythm.move_to，
                # 即平滑轨迹 + 控件内随机点 + 透明层规避，不是瞬移。
                if uia._click_ctrl(target["cell"]):
                    rhythm.nap(1.4)
                    # 只读结果页上出现的控件，绝不点击。**只扫微信窗口**。
                    hits = _scan_wechat(win, _RESULT_PAGE_HINTS)
                    _print_hits(hits, "点击后结果页")
                else:
                    print("[test] 点击结果项失败（控件矩形不可用？）。")

        # ---- 5) 可选：点「添加到通讯录」（打开申请对话框，最终发送绝不点）----
        if do_add:
            btns = _scan_wechat(win, ["添加到通讯录"], type_kw="Button")
            if not btns:
                # 类型不确定（可能渲染成 ListItem/Text），退一步按任意类型找
                btns = _scan_wechat(win, ["添加到通讯录"])
            if not btns:
                print("[test] 微信窗口里没找到「添加到通讯录」按钮，不点击。")
            else:
                nm, _ct, brect, bel = btns[0]
                if brect and not _point_belongs_to_foreground(brect):
                    print("[test] 「%s」落点不属于当前前台窗口，跳过点击。" % nm)
                else:
                    print("[test] 拟人点击：%r" % nm)
                    if uia._click_ctrl(bel):
                        rhythm.nap(1.4)
                        # 把新弹窗先报出来（窗口标题），再读里面的控件——只读不点
                        try:
                            wins = [(_win_title(c) or c.Name or "")
                                    for c in _wechat_windows()]
                            wins = [w for w in wins if w]
                            if wins:
                                print("[test] 当前微信窗口：%s" % " | ".join(wins))
                        except Exception:
                            pass
                        hits = _scan_wechat(win, _ADD_DIALOG_HINTS)
                        _print_hits(hits, "「添加到通讯录」之后")

                        # 「填入」：把常用申请语灌进输入框（不点就发默认的
                        # 「我是中照金智-XX」）。备注框不动。
                        if do_fill:
                            rhythm.nap(0.4)
                            fok, fmsg = _click_fill(uia, win)
                            print("[test] %s" % fmsg)
                            if fok:
                                rhythm.nap(0.5)
                                vals = _edit_values(win)
                                if vals:
                                    print("[test] 对话框输入框当前内容"
                                          "（顺序：申请语 / 备注 / 标签）：")
                                    for v in vals[:3]:
                                        print("    - %s" % (v[:90].replace("\n", " ")))
                                else:
                                    print("[test] （读不到输入框内容，"
                                          "请肉眼确认申请语已替换）")

                        # 点「确定」提交申请（备注框不动；备注只在需要时手动改）
                        if do_confirm:
                            rhythm.nap(0.5)
                            ok, msg = _click_confirm(uia, win)
                            print("[test] %s" % msg)
                            if ok:
                                rhythm.nap(1.5)
                                after = _scan_wechat(
                                    win, ["已发送", "等待验证", "已添加",
                                          "发送成功", "朋友申请"])
                                if after:
                                    _print_hits(after, "提交申请后")
                        else:
                            print("[test] 注意：申请对话框的「确定」**没有点**，"
                                  "需要真正发申请再加 --confirm。")
                    else:
                        print("[test] 点击「添加到通讯录」失败（控件矩形不可用？）。")

        if not keep_open:
            try:
                pyautogui.press("esc")
                rhythm.nap(0.2)
                print("[test] 已按 Esc 收起搜索下拉（用 --keep-open 可留在屏幕上给你看）")
            except Exception:
                pass
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="测微信搜索框能否搜到非联系人（拟人光标轨迹；可选点击结果项）")
    ap.add_argument("--search", required=True,
                    help="要搜的关键词：手机号 / 微信号 / 昵称")
    ap.add_argument("--click", nargs="?", const="网络查找,查找手机", default=None,
                    metavar="关键词",
                    help="点击名称包含该关键词的结果项（多个用逗号分隔，靠前的优先）。"
                         "不带参数默认匹配「网络查找」（即网络查找手机/QQ号）。"
                         "不带本参数则只搜不点。")
    ap.add_argument("--dump-all", action="store_true",
                    help="即使没命中结果，也打印下拉列表全部子项")
    ap.add_argument("--add", action="store_true",
                    help="先点「网络查找」（若未给 --click 则自动启用），"
                         "再点结果页上的「添加到通讯录」，并 dump 弹出的申请对话框。"
                         "对话框里的「确定」默认不点。")
    ap.add_argument("--confirm", action="store_true",
                    help="再点申请对话框的「确定」，真正提交好友申请（隐含 --add）。"
                         "不碰备注框。默认不点，只 dump 对话框。")
    ap.add_argument("--no-fill", action="store_true",
                    help="点「添加到通讯录」后**不**自动点「填入」。"
                         "默认会自动点「填入」，把常用申请语灌进输入框"
                         "（否则发出去是默认的「我是中照金智-XX」）。")
    ap.add_argument("--keep-open", action="store_true",
                    help="结束后不按 Esc，把搜索结果留在屏幕上")
    ap.add_argument("--settle", type=float, default=1.6,
                    help="输入后等结果渲染的秒数（默认 1.6）")
    a = ap.parse_args()
    do_add = a.add or a.confirm          # --confirm 隐含 --add
    needles = None
    if a.click is not None:
        needles = [s.strip() for s in a.click.split(",") if s.strip()]
    elif do_add:
        # 依赖先点「网络查找」；没显式给 --click 就用默认关键词
        needles = ["网络查找", "查找手机"]
    sys.exit(run_search(a.search, keep_open=a.keep_open, settle=a.settle,
                        click_needles=needles, dump_all=a.dump_all,
                        do_add=do_add, do_confirm=a.confirm,
                        do_fill=not a.no_fill))


if __name__ == "__main__":
    main()
