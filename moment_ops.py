#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""朋友圈轻互动模块（点赞 / 评论）——客户维护专用。

架构（两条路线，均已在 2026-10-15/16 本机实测）：
  读：MomentDB（wechatauto 解密 sns.db，零 UI）——水印增量拉新圈 +
      未读赞/评论通知，落 monitor_store，看板展示。
      但纯 DB 读只能看到客户端已拉取的数据（微信 PC 只在用户点朋友圈页
      工具栏「刷新」按钮时才同步），故另有一条 UI 刷新路线 refresh()：
      切到朋友圈页 → 点「刷新」逼客户端同步 → **就地停在朋友圈页**（不切回
      聊天，后续自动互动直接在此定位）；auto_reply 按 moment.refresh_interval
      定期调用（默认 180s，0=关闭）。
  写：Moment（wechatauto UIA，热激活 Qt accessibility gate 后可用）——
      定位(find_moment) → 「…」浮层 → 点赞/评论。实测本机可用
      （点赞/取消赞可逆验证通过；评论发送动作不可逆，留人工确认后真机执行）。

安全闸（写操作，默认最严）：
  0. 按客户自动开关（moment_switch 表，默认全关）——只有看板给某客户开了
     「赞/评」，他的新圈才会被 auto_interact 自动互动；手动点按钮/CLI 是
     人工显式确认，不受此限
  1. dry_run 默认 True——只验证「能定位到目标圈」，不点不发；看板开关可切真跑
  2. 每日上限 config.moment.max_actions_per_day（**0 = 不限次数**，当前默认 0）
     ——2026-10-16 用户拍板取消固定 10 次：正常人朋友圈大一点也不止 10 个，
     而点赞本身是低风险动作、鼠标又是拟人化的。真正的节奏控制交给第 3 条
     rhythm.gate（连发冷却），它才是防「机械连击」的那道闸
  3. 库内置 rhythm.gate 节流（连发强制冷却），与上面叠加

放行标准 = 「开关 / 人工点击」本身，没有白名单：2026-10-16 移除
config.moment.watch——开关即确认，避免「开了开关却被白名单静默挡住」。

用法（手动验证，tid 从 --list 输出拿）：
    python moment_ops.py --list                 # 读：最近新圈 + 未读互动
    python moment_ops.py --like <tid> --dry     # 模拟点赞（只验证定位）
    python moment_ops.py --like <tid> --no-dry  # 真点赞

自动互动（auto_reply.py 轮询内自动执行，无需命令）：poll_new 拉到新圈后，
auto_interact() 按 moment_switch 开关自动点赞/评论——只对开后新到的
圈生效（不补赞旧圈）；自动评论走 ai_comment_text（小光人设）。
"""
import contextlib
import json
import os
import sys
import time

import monitor_store
import ui_gate
import db_gate

# 依赖 wechatauto；本模块被 dashboard / auto_reply 导入时同理。
# 缺库时允许导入失败，看板相关 API 会自己容错。
try:
    from wechatauto import WeChat, WeChatDB, MomentDB
    from wechatauto.moment import Moment
    _HAS_WX = True
except Exception:               # 允许在无 wechatauto 的环境导入（看板 API 容错）
    _HAS_WX = False

ACTIONS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            ".moment_actions.json")     # 每日动作计数（gitignore）

_HUMAN_CURSOR_ANNOUNCED = False


@contextlib.contextmanager
def _human_cursor():
    """在 with 块内，把朋友圈的鼠标动作改走 rhythm 的拟人轨迹。

    背景（2026-10-16 用户反馈「点赞也是直接闪现的」）：wechatauto 库里
    guia.real_click / uia_driver / sender 这些路径都走 ``rhythm.move_to``
    （二次贝塞尔 + 末段减速），**唯独朋友圈这几处漏了**：
      - ``_locate_more_click`` 点「…」用 ``pyautogui.moveTo(..., duration=0.1)``
        ——直线冲刺，没有加减速；
      - ``_click_float_button`` 点「赞/评论」用 ``pyautogui.click(x, y)``
        ——瞬时传送（pyautogui 的 click 内部走 ``_mouseMoveDrag``，不经过
        ``moveTo``，所以两个都得接管）；
      - ``_send_scroll`` 滚时间线前用 ``user32.SetCursorPos`` 直接落位。

    库是 site-packages 里装好的，不宜直接改（重装即丢），所以在**我们这一侧**
    临时接管、用完还原。作用域限定在一次朋友圈写操作内，而朋友圈写操作都在
    UI 门里串行执行，不存在并发冲突。

    注意：接管 ``pyautogui.moveTo/click`` 是**进程级**的，但只有朋友圈这条
    链路会用到带坐标的 pyautogui 点击（消息发送用的是 press/hotkey，不受影响），
    所以实际影响面就是我们要修的这一段。
    """
    global _HUMAN_CURSOR_ANNOUNCED
    try:
        import ctypes
        import pyautogui
        import wechatauto.moment as _wm
        from wechatauto import rhythm
    except Exception as e:                 # 库缺失时静默沿用默认行为
        print("[moment] 拟人光标不可用（沿用库默认）: %s" % e)
        yield
        return

    u = ctypes.windll.user32
    orig_move = pyautogui.moveTo
    orig_click = pyautogui.click
    orig_scroll = getattr(_wm, "_send_scroll", None)

    def _is_xy(a, b):
        return isinstance(a, (int, float)) and isinstance(b, (int, float))

    def moveTo(x=None, y=None, *a, **kw):
        if _is_xy(x, y):
            try:
                rhythm.move_to(u, int(x), int(y))
                return None
            except Exception:
                pass
        return orig_move(x, y, *a, **kw)

    def click(*a, **kw):
        x = a[0] if a else kw.get("x")
        y = a[1] if len(a) > 1 else kw.get("y")
        if _is_xy(x, y):
            try:
                rhythm.move_to(u, int(x), int(y))   # 先平滑走过去
                rhythm.nap(0.15)                    # 落点前停一下再按
            except Exception:
                pass
        return orig_click(*a, **kw)

    if orig_scroll is not None:
        def send_scroll(x, y, delta=-120, times=1, expect_pid=None):
            # 先照原逻辑做「落点不属于微信就别动」的判定，避免平移到别的窗口上
            if expect_pid is not None:
                try:
                    from wechatauto.guia import window_owner_pid
                    owner = window_owner_pid(int(x), int(y))
                    if owner is not None and owner != expect_pid:
                        return orig_scroll(x, y, delta=delta, times=times,
                                           expect_pid=expect_pid)
                except Exception:
                    pass
            try:
                rhythm.move_to(u, int(x), int(y))
            except Exception:
                pass
            return orig_scroll(x, y, delta=delta, times=times, expect_pid=expect_pid)

    pyautogui.moveTo = moveTo
    pyautogui.click = click
    if orig_scroll is not None:
        _wm._send_scroll = send_scroll
    if not _HUMAN_CURSOR_ANNOUNCED:
        _HUMAN_CURSOR_ANNOUNCED = True
        print("[moment] 拟人光标已启用：朋友圈的移动/点击/滚动定位改走 rhythm 轨迹")
    try:
        yield
    finally:
        pyautogui.moveTo = orig_move
        pyautogui.click = orig_click
        if orig_scroll is not None:
            _wm._send_scroll = orig_scroll


def _today():
    return time.strftime("%Y-%m-%d", time.localtime())


def _load_actions():
    try:
        with open(ACTIONS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _bump_actions():
    """今日写动作计数 +1，返回当前计数。"""
    d = _load_actions()
    n = int(d.get(_today(), 0)) + 1
    d[_today()] = n
    # 只保留最近 7 天，防止文件无限增长
    for k in list(d):
        if k < time.strftime("%Y-%m-%d", time.localtime(time.time() - 7 * 86400)):
            del d[k]
    try:
        with open(ACTIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f)
    except Exception:
        pass
    return n


class MomentOps:
    """朋友圈读（DB）+ 写（UI）统一入口。看板 API 与 auto_reply 轮询共用。"""

    def __init__(self, cfg, dry_run=None):
        self.cfg = cfg or {}
        mcfg = self.cfg.get("moment", {}) or {}
        # 每日写动作上限（点赞+评论合计）：<=0 表示**不限次数**（2026-10-16
        # 用户拍板取消固定 10 次）。仍保留计数（_bump_actions）供看板显示今日用量。
        self.max_actions = int(mcfg.get("max_actions_per_day", 0))
        self.ai_comment = bool(mcfg.get("ai_comment", True))
        # dry_run：None=看板开关（conv_switch 表 '__moment_dry__'，默认 1=模拟）
        if dry_run is None:
            switches = monitor_store.get_switches()
            self.dry_run = bool(switches.get("__moment_dry__", 1))
        else:
            self.dry_run = bool(dry_run)
        self._db = None
        self._mdb = None
        self._wx = None
        self._moment = None
        self.last_new_feeds = []     # 最近一次 poll_new 新到的圈（auto_interact 消费）

    # ---------- 惰性依赖 ----------
    @property
    def db(self):
        if self._db is None:
            self._db = db_gate.get_db()
        return self._db

    @property
    def mdb(self):
        if self._mdb is None:
            self._mdb = MomentDB(self.db)
        return self._mdb

    @property
    def wx(self):
        # 进程内复用同一个 WeChat 实例：构造有成本（热激活校验等），
        # refresh / moment 属性 / 写操作都会用到。
        if self._wx is None:
            self._wx = WeChat()
        return self._wx

    @property
    def moment(self):
        if self._moment is None:
            self._moment = self.wx.Moment
            if self._moment is None or not self.wx.SwitchToMoments():
                raise RuntimeError(
                    "朋友圈界面不可用（热激活未成功/微信未登录）。"
                    "读路线不受影响，写操作暂不可用。")
        return self._moment

    def _ensure_foreground(self):
        """写操作前把个人微信主窗口激活到真正前台。

        为什么必须做：库的 SwitchToMoments / find_moment 靠 UIA 点击
        「发现→朋友圈」导航，主窗口不在前台时点击落空——2026-10-16 实测，
        auto_reply 后台进程里热激活成功、树也物化了，但 SNSWindow 始终
        不出现，定位直接失败（日志 3 次「未找到朋友圈窗口」后放弃）。
        手动跑时微信在前台所以一直正常，自动化场景必须自己提前台。

        用 wechat_ui.focus_wechat（自带浏览器/企业微信窗口排除 + 前台锁
        解除组合技）。失败不致命：打印后继续，由后续定位结果兜底。

        已在前台则直接跳过：refresh 刚聚焦过时重复枚举会撞上瞬时竞态
        （2026-10-16 实测：切回聊天页后立刻枚举不到窗口，其实前台就是微信）。
        """
        try:
            import wechat_ui
            if wechat_ui.wechat_is_foreground():
                return None              # 已在前台，免枚举免激活
            hwnd, title = wechat_ui.focus_wechat()
            print("[moment] 已激活微信窗口到前台：%s" % title)
            return hwnd
        except Exception as e:
            print("[moment] 激活微信窗口失败（将继续尝试定位）: %s" % e)
            return None

    def _ensure_moments_page(self):
        """确保当前停在朋友圈页（写操作定位的前提）。返回 True=已确认在页面。

        为什么必须：`moment` 属性只在**首次初始化**时 SwitchToMoments；
        若期间页面被切走（键盘发送前会切回聊天页、用户手动操作），
        find_moment 会在聊天页面上白跑——2026-10-16 自动点赞全失败的
        实锤：库打「滚轮落点不属于微信 (0,0)」「未能定位到目标朋友圈」。
        SwitchToMoments 自带 _has_timeline 检查，已在朋友圈页时 0.1s 空转，
        可安全重复调用；它点「发现→朋友圈」导航，需要微信在前台
        （调用方已先 _ensure_foreground）。

        并发加固（2026-10-16）：本实例的 WeChat/Moment 控件是长生命周期缓存，
        而 UI 门是**跨进程**的——另一个进程（看板）操作过界面后，本进程缓存的
        控件引用可能已失效（切换页面会让 UIA 元素树重建）。所以首次失败时
        丢弃缓存重建一次再试，避免把「对方刚切过页」误判成「定位失败」。
        """
        for attempt in (1, 2):
            try:
                if self.wx.SwitchToMoments():
                    return True
            except Exception as e:
                print("[moment] 切换朋友圈页异常（第 %d 次）: %s" % (attempt, e))
            if attempt == 1:
                # 缓存可能已被另一进程的操作搞失效：清掉重建再试一次
                self._moment = None
                self._wx = None
                print("[moment] 朋友圈控件缓存疑似失效（别的进程切过页），重建后重试")
        print("[moment] 切换朋友圈页失败（热激活未成功/微信未登录？）")
        return False

    # ---------- 读：水印增量 ----------
    def _download_media(self, feed):
        """把一条圈的图片下载到 wechat_media/moments/<tid>/，本地路径按 md5 精确写回。
        best-effort：失败不影响主流程（媒体是微信加密缓存，解密失败就只显示文本）。
        逐个下载而不用 download_moment_media——后者只返回路径列表，某张失败会错位。"""
        imgs = feed.get("images") or []
        if not imgs:
            return
        try:
            media_root = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      "wechat_media")
            target = os.path.join(media_root, "moments",
                                  "%s_%s" % (feed.get("tid") or feed.get("username") or "sns",
                                             feed.get("id") or ""))
            for img in imgs:
                if img.get("local"):
                    continue                     # 已有本地路径（幂等重跑）
                try:
                    res = self.mdb.download_media_detailed(img, target, "image")
                except Exception:
                    continue
                p = res.get("path")
                if p and os.path.isfile(p):
                    img["local"] = os.path.relpath(p, media_root).replace("\\", "/")
        except Exception:
            return

    def poll_new(self):
        """轮询：新到的朋友圈 + 未读互动通知，落库。返回统计 dict。

        两个库级坑，都绕开（2026-10-15 实测确认）：
        ① SQL tid 列全是负整数、与新旧无关（真正的自增 ID 在 XML <id> 里，
           正数递增），所以 ORDER BY tid DESC 排的不是最新圈，
           get_moments_since/latest_tid 的水印语义整个不可信；
        ② 微信 20 位 ID 超 int64，SQLite 列里 integer/real/text 混存。
        因此：全量拉取 -> Python int 按 XML id 排序 -> 水印过滤。
        朋友圈库量级小（几百条），全量解析成本可接受；量大了再加
        create_time 粗筛。
        """
        if not _HAS_WX:
            return {"new_moments": 0, "new_inter": 0, "error": "wechatauto 不可用"}
        stat = {"new_moments": 0, "new_inter": 0}
        cursor = monitor_store.get_moment_cursor()
        try:
            cursor_int = int(cursor)
        except (TypeError, ValueError):
            cursor_int = 0
        try:
            feeds = self.mdb.get_moments(limit=0)     # 全量（内部 DESC 不可信，本地重排）
        except Exception as e:
            stat["error"] = "读取朋友圈失败: %s" % e
            return stat
        # 本地按 XML id（真正的自增序列）排序，再取最近 200 条
        def _tid(f):
            try:
                return int(str(f.get("id") or 0))
            except ValueError:
                return 0
        feeds.sort(key=_tid, reverse=True)
        feeds = feeds[:200]
        # 水印过滤：tid > cursor（Python int 任意精度，不吃 int64 溢出）
        new_feeds = []
        max_tid = cursor_int
        for f in feeds:
            tid_int = _tid(f)
            if tid_int > cursor_int:
                new_feeds.append(f)
            if tid_int > max_tid:
                max_tid = tid_int
        self.last_new_feeds = new_feeds     # 供 auto_interact 消费（只含本次新到的）
        for f in new_feeds:
            # followed 恒为 1：看板展示全部新圈（2026-10-16 移除 watch 白名单，
            # 「关注谁」由 moment_switch 开关决定，不再由配置过滤展示）
            self._download_media(f)
            if monitor_store.add_moment(f, 1):
                stat["new_moments"] += 1
                print("[moment] + 新圈 %s: %s" % (
                    f.get("nickname") or f.get("username") or "",
                    (f.get("text") or "")[:40].replace("\n", " ")))
        if max_tid > cursor_int:
            monitor_store.set_moment_cursor(str(max_tid))
        # 互动通知（别人赞/评论我）
        try:
            inters = self.mdb.get_interactions(limit=50, only_unread=True)
        except Exception:
            inters = []
        for it in inters:
            if monitor_store.add_interaction(it):
                stat["new_inter"] += 1
                print("[moment] + 互动通知 %s %s" % (
                    it.get("from_nickname") or it.get("from_username"),
                    "赞了你" if it.get("type") == 1 else "评论了你"))
        return stat

    # ---------- UI 刷新：逼客户端同步新圈 ----------
    def _find_refresh_button(self):
        """在朋友圈页找工具栏「刷新」按钮（mmui::XTabBarItem，Name='刷新'）。

        2026-10-16 探针实测：朋友圈页顶部是 mmui::SNSWindowToolBar，内含
        「消息 / 发表 / 刷新 / 独立窗口显示」四个 XTabBarItem 按钮——
        用户确认「必须点刷新才会拉新圈」。库 languages.py 里有「刷新」
        词条但自己没接，这里自己走 UIA 按 Name 找（与找「赞/评论」同款）。
        返回 uia Control；找不到返回 None。
        """
        try:
            from wechatauto.languages import MOMENTS, get_lang
            target = get_lang(MOMENTS, '刷新')
        except Exception:
            target = '刷新'
        try:
            # control 是懒加载的：必须先调一次 GetMoments 触发 _ensure_list
            # （_find_sns_window 带 3s 重试等待 SNSContentView 出现），
            # 直接读 .control 在没初始化过时永远是 None（2026-10-16 踩到）。
            self.moment.GetMoments()
            root = self.moment.control
        except Exception:
            return None
        if root is None:
            return None
        found = []

        def walk(c, depth):
            if found or depth > 12:
                return
            try:
                kids = c.GetChildren()
            except Exception:
                return
            for k in kids:
                if found:
                    return
                try:
                    if (getattr(k, 'ControlTypeName', '') == 'ButtonControl'
                            and (getattr(k, 'Name', '') or '').strip() == target):
                        found.append(k)
                        return
                    walk(k, depth + 1)
                except Exception:
                    continue

        walk(root, 0)
        return found[0] if found else None

    def _click_refresh_button(self):
        """点一下朋友圈页工具栏的「刷新」按钮（拟人坐标 + 节流点击）。

        返回 True=点了；False=没找到或点击失败（调用方降级为只切页等待）。
        """
        btn = None
        for _ in range(10):                   # 页面切换有动画，工具栏慢慢渲染
            btn = self._find_refresh_button()
            if btn is not None:
                break
            time.sleep(0.5)
        if btn is None:
            print("[moment] 未找到「刷新」按钮（工具栏未渲染？），降级为仅切页等待")
            return False
        try:
            import ctypes
            import pyautogui
            from wechatauto import rhythm
            r = btn.BoundingRectangle
            rect = (r.left, r.top, r.right, r.bottom)
            x, y = rhythm.point(rect)         # 控件内随机点，避开正中心
            # 平滑移动再点：朋友圈页工具栏「刷新」在顶部，而光标此刻还停在导航
            # 「发现」附近，SetCursorPos 一步闪现是脚本签名（2026-10-16 用户反馈
            # 「闪现过去不对」）。rhythm.move_to 走二次贝塞尔 + 随机弓形 + 末段
            # 减速，以真实光标为起点，落点前再 nap 一下（与库 guia.real_click 同款）；
            # off 档会退回直接传送（对照实验用，自然档 steps=(6,14) 有过程）。
            rhythm.move_to(ctypes.windll.user32, x, y)
            rhythm.nap(0.15)
            # 不调 rhythm.gate：gate 最冷要睡 30-75s，那期间微信一直占着前台；
            # 且点「刷新」是导航/读动作（不对外发布任何内容），按库自己的
            # 规矩「读和定位不节流」。
            pyautogui.click()                 # 光标已停在 (x,y)，仅按下抬起，不再传送
            print("[moment] 已点击朋友圈页「刷新」按钮 (%d,%d)，等待客户端同步…"
                  % (x, y))
            return True
        except Exception as e:
            print("[moment] 点击「刷新」按钮失败: %s" % e)
            return False

    def refresh(self, dwell=None):
        """UI 路线刷新朋友圈：切到朋友圈页 → 点工具栏「刷新」→ 停留（就地停在朋友圈页）。

        为什么必须**点刷新**而不只是切页（2026-10-16 用户实测 + 探针确认）：
        微信 PC 只在用户点朋友圈页工具栏的「刷新」按钮时才向服务端拉新圈；
        光切过去停留，时间线仍是旧数据，本地 sns.db 不更新，poll_new 照样
        0 条——这就是「必须人工点开一次朋友圈才落库」的根因（切页不够）。
        按钮在朋友圈页顶部 mmui::SNSWindowToolBar（Name='刷新' 的
        XTabBarItem，探针实测坐标约 (3134,775)-(3204,824)，以实时读取为准）。

        **不切回聊天页**（2026-10-16 用户拍板「返回聊天这步不需要，没必要」）：
        刷新后紧接着的自动互动本就要在朋友圈页定位，切回聊天再切回来纯属
        多余，且正是当天自动点赞全失败的根因（find_moment 在聊天页白跑，
        库打「滚轮落点不属于微信 (0,0)」）。唯一需要回聊天页的地方是
        「发消息前的 Ctrl+F」，由 wechat_ui.send_to_contact 自己保证。

        best-effort：任何一步失败只打印返回 False，不影响主流程。

        并发：整段（聚焦→切页→点刷新→等同步→物化时间线）在 **UI 门**内执行。
        微信只有一份 UI，刷新期间必须独占前台，否则会被消息发送打断切回
        聊天页——那正是 2026-10-16 自动点赞全失败的根因。拿不到门就排队等，
        等超时（UI 被别的操作长时间占用）只打印返回 False，不抛。
        """
        if not _HAS_WX:
            return False
        if dwell is None:
            dwell = float((self.cfg.get("moment", {}) or {}).get("refresh_dwell", 4))
        try:
            with ui_gate.hold("刷新朋友圈"):
                import wechat_ui
                wechat_ui.focus_wechat()      # 库的 UIA 导航要求主窗口真前台
                if not self.wx.SwitchToMoments():
                    print("[moment] 刷新失败：未能切换到朋友圈页（热激活/未登录？）")
                    return False
                self._click_refresh_button()  # 关键：逼客户端向服务端拉新圈
                time.sleep(dwell)             # 等客户端拉取并写入本地 sns.db
                try:
                    # 物化时间线控件（顺带确认页面对了）；失败不影响已同步的数据
                    self.moment.GetMoments(refresh=True)
                except Exception as e:
                    print("[moment] 刷新：时间线物化失败（不影响已同步的数据）: %s" % e)
            print("[moment] 刷新完成：客户端已同步（停在朋友圈页，"
                  "紧随其后的自动互动直接在此定位）")
            return True
        except ui_gate.UITimeoutError as e:
            print("[moment] 刷新取消：%s（微信被别的操作长时间占用）" % e)
            return False
        except Exception as e:
            print("[moment] 刷新异常: %s" % e)
            return False

    # ---------- 自动互动（按客户开关，默认全关） ----------
    def auto_interact(self, feeds=None):
        """对「本次轮询新到」的圈执行自动点赞/评论。

        与消息自动回复同款语义（moment_switch 表，按客户两个开关，默认关）：
        - 只处理本次新到的圈——开开关不回溯历史，不会批量补赞旧圈；
        - 放行标准就是开关本身（2026-10-16 移除了 watch 白名单：开关即确认），
          之后仍走 like()/comment() 的整套闸：dry-run（默认模拟，只验证
          定位）→ 每日上限（`max_actions_per_day`，0=不限）→ rhythm 节流；
        - 自动评论由小光人设生成文案（config.moment.ai_comment=false 时跳过），
          评论不可逆，所以它的开关独立且默认关。
        返回 {liked, commented, skipped, errors}。
        """
        stat = {"liked": 0, "commented": 0, "skipped": 0, "errors": 0}
        if feeds is None:
            feeds = self.last_new_feeds or []
        if not feeds:
            return stat
        switches = monitor_store.get_moment_switches()

        # ---- 阶段 1（UI 门外）：判定 + AI 评论文案生成 ----
        # AI 生成是网络请求（数秒级），绝不能占着 UI 门做——否则这段时间
        # 所有消息自动回复、看板手动发送都得干等，把「UI 只有一个」的瓶颈
        # 从几秒放大到几十秒。所以文案先全部生成好，再进 UI 门一次执行。
        plan = []
        for f in feeds:
            username = f.get("username") or ""
            tid = str(f.get("id") or "")
            sw = switches.get(username) or {}
            want_like = bool(sw.get("auto_like"))
            want_comment = bool(sw.get("auto_comment")) and self.ai_comment
            if not tid or not (want_like or want_comment):
                continue
            name = f.get("nickname") or username
            item = {"tid": tid, "name": name,
                    "preview": (f.get("text") or "")[:30].replace("\n", " "),
                    "like": want_like, "comment": None}
            if want_comment:
                gen = self.ai_comment_text(tid)
                if gen.get("ok") and gen.get("text"):
                    item["comment"] = gen["text"]
                else:
                    stat["skipped"] += 1
                    print("[moment] 自动评论跳过 %s: %s" % (name, gen.get("reason")))
            plan.append(item)

        # ---- 阶段 2（UI 门内）：逐条执行 ----
        # 每条各自持门（like()/comment() 内部包了 ui_gate.hold），条与条之间
        # 让出 UI：消息自动回复、看板手动操作能插进来排队。不整批持门的理由
        # 是别让一批 10 条点赞把微信独占一两分钟；代价是被插队后下一条要重新
        # 导航回朋友圈页（_ensure_moments_page 兜底，约 1~2s）。
        #
        # 任务级超时（用户 2026-10-16：「每个点赞算一条任务」）：一条「赞+评」
        # 是**一个任务**，带自己的时间预算——预算内排到 UI 门就干，超了这条
        # 作废（把剩余预算当 UI 门的排队上限即可，排队是唯一不可控的耗时）。
        task_timeout = float((self.cfg.get("moment", {}) or {}).get(
            "task_timeout_seconds", 120))
        for p in plan:
            deadline = time.time() + task_timeout
            if p["like"]:
                r = self.like(p["tid"], timeout=max(1.0, deadline - time.time()))
                if r.get("ok"):
                    stat["liked"] += 1
                    print("[moment] 自动%s %s: %s" % (
                        "模拟点赞" if r.get("dry") else "点赞", p["name"], p["preview"]))
                else:
                    stat["errors"] += 1
                    print("[moment] 自动点赞未执行 %s: %s" % (p["name"], r.get("reason")))
            if p["comment"]:
                r = self.comment(p["tid"], p["comment"],
                                 timeout=max(1.0, deadline - time.time()))
                if r.get("ok"):
                    stat["commented"] += 1
                    print("[moment] 自动%s评论 %s: %s" % (
                        "模拟" if r.get("dry") else "", p["name"], p["comment"][:30]))
                else:
                    stat["errors"] += 1
                    print("[moment] 自动评论未执行 %s: %s" % (p["name"], r.get("reason")))
        return stat

    # ---------- 写：安全闸 ----------
    def _write_gate(self, tid):
        """写操作前置校验。返回 (放行?, 原因, 目标feed or None)。

        放行标准 = 人工显式确认：手动是看板按钮/CLI 调用本身，自动是
        auto_interact 里该客户开了对应开关。这里只做共性校验：
        目标在库 → dry-run → 每日上限（`max_actions<=0` 时不限次数，跳过此闸）。
        （2026-10-16 移除了 watch 白名单——开关即确认，避免双重标准。）
        """
        feed = None
        for m in monitor_store.get_moments(limit=200):
            if str(m.get("tid")) == str(tid):
                feed = m
                break
        if not feed:
            return False, "目标朋友圈不在库中（先跑一次 poll_new）", None
        if self.dry_run:
            return True, "dry-run 模式：只验证定位，不真点", feed
        n = int(_load_actions().get(_today(), 0))
        if self.max_actions > 0 and n >= self.max_actions:
            return False, "今日朋友圈动作已达上限（%d/%d）" % (n, self.max_actions), None
        if self.max_actions > 0:
            return True, "放行（今日第 %d/%d 次）" % (n + 1, self.max_actions), feed
        return True, "放行（今日第 %d 次，不限量）" % (n + 1), feed

    def like(self, tid, cancel=False, timeout=None):
        """点赞/取消点赞指定 tid 的朋友圈。tid 必须在库中（先 poll_new）。
        timeout：在 UI 门上排队等待的上限（秒）＝ 调用方给这条**任务**的剩余
        时间预算（用户 2026-10-16：「每个点赞算一条任务」）；省略用全局默认。

        并发：定位+点击整段在 **UI 门**内串行（跨进程互斥）——点赞要滚时间线、
        点浮层，必须独占前台。拿不到门排队等；等超时返回 ok=False（不抛）。
        """
        ok, reason, feed = self._write_gate(tid)
        if not ok:
            return {"ok": False, "reason": reason}
        publisher = feed.get("nickname") or ""
        try:
            with ui_gate.hold("点赞 %s" % (publisher or tid[-8:]), timeout=timeout):
                self._ensure_foreground()  # 库的 UIA 点击导航需要微信在前台
                self._ensure_moments_page()  # 且必须停在朋友圈页（被切回过聊天就得再导航）
                # 库的点赞链路（滚动定位 → 点「…」→ 点浮层「赞」）默认是瞬时/直线
                # 移动，_human_cursor 期间改成 rhythm 拟人轨迹（用完还原）。
                with _human_cursor():
                    if self.dry_run:
                        located = self._locate_only(publisher)
                        return {"ok": bool(located), "dry": True, "located": bool(located),
                                "reason": reason, "publisher": publisher,
                                "detail": "dry-run：定位%s，未发送点赞" % ("成功" if located else "失败")}
                    if cancel:
                        # LikeMoment 无 cancel 参数，取消赞需先定位再 Like(item, cancel=True)
                        item = self.moment.find_moment(publisher=publisher)
                        if item is None:
                            return {"ok": False, "reason": "未能定位到目标朋友圈",
                                    "publisher": publisher}
                        r = self.moment.Like(item, cancel=True)
                    else:
                        r = self.moment.LikeMoment(publisher=publisher)
                _bump_actions()
                return {"ok": bool(getattr(r, "is_success", False)),
                        "reason": str(getattr(r, "message", r)),
                        "publisher": publisher}
        except ui_gate.UITimeoutError as e:
            return {"ok": False, "reason": "等待微信 UI 空闲超时：%s" % e}
        except Exception as e:
            return {"ok": False, "reason": "点赞异常: %s" % e}

    def comment(self, tid, content, timeout=None):
        """评论指定 tid 的朋友圈（不可逆动作，务必人工确认内容）。
        timeout：同 like()，调用方给这条任务在 UI 门上排队的剩余预算。

        并发：同 like()，定位+发送整段在 UI 门内串行。
        """
        if not content or not content.strip():
            return {"ok": False, "reason": "评论内容不能为空"}
        ok, reason, feed = self._write_gate(tid)
        if not ok:
            return {"ok": False, "reason": reason}
        publisher = feed.get("nickname") or ""
        try:
            with ui_gate.hold("评论 %s" % (publisher or tid[-8:]), timeout=timeout):
                self._ensure_foreground()  # 库的 UIA 点击导航需要微信在前台
                self._ensure_moments_page()  # 且必须停在朋友圈页（被切回过聊天就得再导航）
                with _human_cursor():      # 同 like()：整段改走拟人光标轨迹
                    if self.dry_run:
                        located = self._locate_only(publisher)
                        return {"ok": bool(located), "dry": True, "located": bool(located),
                                "reason": reason, "publisher": publisher,
                                "detail": "dry-run：定位%s，未发送评论「%s」" % (
                                    "成功" if located else "失败", content[:20])}
                    r = self.moment.CommentMoment(publisher=publisher, content=content)
                _bump_actions()
                return {"ok": bool(getattr(r, "is_success", False)),
                        "reason": str(getattr(r, "message", r)),
                        "publisher": publisher}
        except ui_gate.UITimeoutError as e:
            return {"ok": False, "reason": "等待微信 UI 空闲超时：%s" % e}
        except Exception as e:
            return {"ok": False, "reason": "评论异常: %s" % e}

    def _locate_only(self, publisher, retries=2):
        """dry-run：只验证目标圈能被定位（find_moment 会滚动时间线）。

        带一次重试：库的「点发现→朋友圈」导航在窗口刚提前台时可能还没
        稳定（页面切换+树物化有延迟），首次定位失败后重新激活窗口再试
        一次，避免把「慢」当「失败」。retries=总尝试次数。
        """
        for attempt in range(1, retries + 1):
            try:
                it = self.moment.find_moment(publisher=publisher)
                if it is not None:
                    return True
            except Exception as e:
                print("[moment] 定位异常（第 %d/%d 次）: %s" % (attempt, retries, e))
            if attempt < retries:
                print("[moment] 定位未命中，重新激活微信窗口后重试（%d/%d）"
                      % (attempt + 1, retries))
                self._ensure_foreground()
        return False

    # ---------- AI 评论文案 ----------
    def ai_comment_text(self, tid):
        """给指定朋友圈生成一条 AI 评论文案（复用销售人设 + 知识库）。"""
        feed = None
        for m in monitor_store.get_moments(limit=200):
            if str(m.get("tid")) == str(tid):
                feed = m
                break
        if not feed:
            return {"ok": False, "reason": "目标不在库中"}
        try:
            import ai_client
            intent = (
                "给客户的这条朋友圈写一条评论。朋友圈内容：「%s」\n"
                "要求：1-2 句、简短真诚、像真人朋友/顾问的语气，"
                "不营销腔、不堆表情、不阿谀；可以结合我们产品（照明设计 AI 工具）"
                "自然关联，但不要硬广。只输出评论正文。"
                % (feed.get("text") or "")[:200])
            contact = {"name": feed.get("nickname") or "", "tags": [], "note": ""}
            text = ai_client.generate(self.cfg, contact, intent)
            return {"ok": True, "text": (text or "").strip()}
        except Exception as e:
            return {"ok": False, "reason": "AI 生成失败: %s" % e}


# ==================== CLI（手动验证用） ====================

def main():
    import argparse
    ap = argparse.ArgumentParser(description="朋友圈轻互动（点赞/评论）")
    ap.add_argument("--list", action="store_true", help="读：最近新圈 + 未读互动")
    ap.add_argument("--poll", action="store_true", help="跑一次轮询（水印增量）")
    ap.add_argument("--refresh", action="store_true",
                    help="UI 路线刷新：切到朋友圈页逼客户端同步新圈（刷完就地停在朋友圈页）")
    ap.add_argument("--like", metavar="TID", help="点赞指定 tid 的朋友圈")
    ap.add_argument("--unlike", metavar="TID", help="取消点赞")
    ap.add_argument("--comment", nargs=2, metavar=("TID", "TEXT"), help="评论")
    ap.add_argument("--ai-comment", metavar="TID", help="AI 生成评论文案")
    ap.add_argument("--dry", action="store_true", default=True,
                    help="dry-run：只验证定位不真点（默认）")
    ap.add_argument("--no-dry", dest="dry", action="store_false", help="真执行")
    args = ap.parse_args()

    monitor_store.init()
    cfg = {}
    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    if os.path.isfile(cfg_path):
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)

    ops = MomentOps(cfg, dry_run=args.dry)

    if args.list:
        ms = monitor_store.get_moments(limit=30)
        print("最近新圈 %d 条：" % len(ms))
        for m in ms:
            print("  [%s] %s: %s" % (m["tid"][-8:], m["nickname"],
                                     (m["text"] or "")[:40].replace("\n", " ")))
        inters = monitor_store.get_interactions(limit=20)
        print("互动通知 %d 条（未读 %d）：" % (
            len(inters), monitor_store.moment_unread_count()))
        for it in inters:
            print("  %s %s (%s)" % (
                it["from_nickname"],
                "赞了你" if it["type"] == 1 else "评论: %s" % it["content"],
                "未读" if it["unread"] else "已读"))
        return
    if args.poll:
        print(ops.poll_new())
        return
    if args.refresh:
        print("ok" if ops.refresh() else "failed")
        return
    if args.like:
        print(ops.like(args.like))
        return
    if args.unlike:
        print(ops.like(args.unlike, cancel=True))
        return
    if args.comment:
        print(ops.comment(args.comment[0], args.comment[1]))
        return
    if args.ai_comment:
        print(ops.ai_comment_text(args.ai_comment))
        return
    ap.print_help()


if __name__ == "__main__":
    main()
