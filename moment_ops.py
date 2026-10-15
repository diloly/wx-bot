#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""朋友圈轻互动模块（点赞 / 评论）——客户维护专用。

架构（两条路线，均已在 2026-10-15 本机实测）：
  读：MomentDB（wechatauto 解密 sns.db，零 UI）——水印增量拉关注客户新圈 +
      未读赞/评论通知，落 monitor_store，看板展示。
  写：Moment（wechatauto UIA，热激活 Qt accessibility gate 后可用）——
      定位(find_moment) → 「…」浮层 → 点赞/评论。实测本机可用
      （点赞/取消赞可逆验证通过；评论发送动作不可逆，留人工确认后真机执行）。

安全闸（写操作四道，全部默认最严）：
  1. dry_run 默认 True——只验证「能定位到目标圈」，不点不发；看板开关可切真跑
  2. 白名单——只允许操作「关注客户（config.moment.watch）或自己」的圈，
     绝不碰任何其他人的朋友圈（真实社交动作，不可逆）
  3. 每日上限 config.moment.max_actions_per_day（默认 10），防朋友圈风控
     （朋友圈反垃圾独立于聊天风控，批量互动是公认高危）
  4. 库内置 rhythm.gate 节流（连发强制冷却），与上面叠加

用法（手动验证）：
    python moment_ops.py --list                 # 读：最近新圈 + 未读互动
    python moment_ops.py --like "中照金智-小骆" --dry     # 模拟点赞自己的圈
    python moment_ops.py --like "中照金智-小骆" --no-dry  # 真点赞（需白名单放行）
"""
import json
import os
import sys
import time

import monitor_store

# wechatauto 装在 your-env 环境；本模块被 dashboard/auto_reply  import 时同理
try:
    from wechatauto import WeChat, WeChatDB, MomentDB
    from wechatauto.moment import Moment
    _HAS_WX = True
except Exception:               # 允许在无 wechatauto 的环境导入（看板 API 容错）
    _HAS_WX = False

ACTIONS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            ".moment_actions.json")     # 每日动作计数（gitignore）


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
        self.watch = set(mcfg.get("watch", []) or [])     # 关注客户 wxid（空=只读不写）
        self.max_actions = int(mcfg.get("max_actions_per_day", 10))
        self.ai_comment = bool(mcfg.get("ai_comment", True))
        # dry_run：None=看板开关（conv_switch 表 '__moment_dry__'，默认 1=模拟）
        if dry_run is None:
            switches = monitor_store.get_switches()
            self.dry_run = bool(switches.get("__moment_dry__", 1))
        else:
            self.dry_run = bool(dry_run)
        self._db = None
        self._mdb = None
        self._moment = None

    # ---------- 惰性依赖 ----------
    @property
    def db(self):
        if self._db is None:
            self._db = WeChatDB()
        return self._db

    @property
    def mdb(self):
        if self._mdb is None:
            self._mdb = MomentDB(self.db)
        return self._mdb

    @property
    def moment(self):
        if self._moment is None:
            wx = WeChat()
            self._moment = wx.Moment
            if self._moment is None or not wx.SwitchToMoments():
                raise RuntimeError(
                    "朋友圈界面不可用（热激活未成功/微信未登录）。"
                    "读路线不受影响，写操作暂不可用。")
        return self._moment

    @property
    def my_wxid(self):
        return getattr(self.db, "wxid", "") or ""

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
        """轮询：关注客户的新圈 + 未读互动通知，落库。返回统计 dict。

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
            return {"new_moments": 0, "followed": 0, "new_inter": 0, "error": "wechatauto 不可用"}
        stat = {"new_moments": 0, "followed": 0, "new_inter": 0}
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
        for f in new_feeds:
            username = f.get("username") or ""
            followed = 1 if (not self.watch or username in self.watch) else 0
            if followed:
                self._download_media(f)
            if monitor_store.add_moment(f, followed):
                stat["new_moments"] += 1
                if followed:
                    stat["followed"] += 1
                    print("[moment] + 关注客户新圈 %s: %s" % (
                        f.get("nickname") or username,
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

    # ---------- 写：安全闸 ----------
    def _write_gate(self, tid):
        """写操作前置校验。返回 (放行?, 原因, 目标feed or None)。

        白名单按 wxid 精确校验：只允许「关注客户（config.moment.watch）
        或本人」的圈。绝不按昵称猜——昵称会重名。
        """
        feed = None
        for m in monitor_store.get_moments(limit=200):
            if str(m.get("tid")) == str(tid):
                feed = m
                break
        if not feed:
            return False, "目标朋友圈不在库中（先跑一次 poll_new）", None
        username = feed.get("username") or ""
        if username != self.my_wxid and username not in self.watch:
            return False, ("白名单未放行：%s(%s) 不是关注客户/本人，"
                           "不碰其他人的朋友圈" % (feed.get("nickname"), username)), None
        if self.dry_run:
            return True, "dry-run 模式：只验证定位，不真点", feed
        n = int(_load_actions().get(_today(), 0))
        if n >= self.max_actions:
            return False, "今日朋友圈动作已达上限（%d/%d）" % (n, self.max_actions), None
        return True, "放行（今日第 %d/%d 次）" % (n + 1, self.max_actions), feed

    def like(self, tid, cancel=False):
        """点赞/取消点赞指定 tid 的朋友圈。tid 必须在库中且过白名单。"""
        ok, reason, feed = self._write_gate(tid)
        if not ok:
            return {"ok": False, "reason": reason}
        publisher = feed.get("nickname") or ""
        if self.dry_run:
            located = self._locate_only(publisher)
            return {"ok": bool(located), "dry": True, "located": bool(located),
                    "reason": reason, "publisher": publisher,
                    "detail": "dry-run：定位%s，未发送点赞" % ("成功" if located else "失败")}
        try:
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
        except Exception as e:
            return {"ok": False, "reason": "点赞异常: %s" % e}

    def comment(self, tid, content):
        """评论指定 tid 的朋友圈（不可逆动作，务必人工确认内容）。"""
        if not content or not content.strip():
            return {"ok": False, "reason": "评论内容不能为空"}
        ok, reason, feed = self._write_gate(tid)
        if not ok:
            return {"ok": False, "reason": reason}
        publisher = feed.get("nickname") or ""
        if self.dry_run:
            located = self._locate_only(publisher)
            return {"ok": bool(located), "dry": True, "located": bool(located),
                    "reason": reason, "publisher": publisher,
                    "detail": "dry-run：定位%s，未发送评论「%s」" % (
                        "成功" if located else "失败", content[:20])}
        try:
            r = self.moment.CommentMoment(publisher=publisher, content=content)
            _bump_actions()
            return {"ok": bool(getattr(r, "is_success", False)),
                    "reason": str(getattr(r, "message", r)),
                    "publisher": publisher}
        except Exception as e:
            return {"ok": False, "reason": "评论异常: %s" % e}

    def _locate_only(self, publisher):
        """dry-run：只验证目标圈能被定位（find_moment 会滚动时间线）。"""
        try:
            it = self.moment.find_moment(publisher=publisher)
            return it is not None
        except Exception:
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
        ms = monitor_store.get_moments(limit=30, only_followed=True)
        print("关注客户新圈 %d 条：" % len(ms))
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
