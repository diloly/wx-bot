#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tag_sync.py —— 微信好友标签「同步一次」，把成员归属缓存进看板库。

背景（2026-11-23 实测确认）：
    微信 4.x PC 的标签「定义」存在 contact.db 的 contact_label（能纯库读），但
    「谁属于哪个标签」这份映射**本地库里没有**（extra_buffer 逐字段解过也没对应
    字段，库作者扫过 409 个联系人结论一致）。唯一能拿到的路径是驱动微信界面：
    通讯录 → 通讯录管理 → 点每个标签 → 读右侧成员列表。

设计（本项目硬约束）：
    * **只在手动同步时抢一次 UI**：读界面全部包在 ``ui_gate.hold`` 里，和自动
      回复/发消息/朋友圈共用一把门，绝不并发。
    * **选人时零抢占**：读回来的 {wxid: [标签]} 存进 monitor_store.contact_tag，
      看板所有选人入口只读这份缓存，不再碰微信界面。
    * **DB 写在门之外**：门只护 UI，拿完数据就放锁，再落库。

对照 wechatauto.labels 的口径：成员行要 resolve_members 拿 wxid，重名/查不到的
进 skipped 不猜人；标签行自带人数 ``count``，和读到数对不上时 complete=False——
这里如实回报，不假装同步全了。空标签（no-list 且 count=0）算正常空结果。

用法::
    import tag_sync
    result = tag_sync.sync()      # 在 ui_gate 内读、门外写，返回结果 dict
"""
import time


class TagSyncError(RuntimeError):
    """同步前置条件不满足（微信库/依赖/UI 不可用）。"""


def _labels_ops():
    """拿 wechatauto 的 LabelOps。依赖缺失时给人话错误，不抛栈。"""
    try:
        from wechatauto import WeChat
    except ImportError:
        raise TagSyncError("缺少 wechatauto-replica，无法读微信标签")
    try:
        wx = WeChat()
        ops = wx._labels()
    except Exception as e:              # noqa: BLE001
        raise TagSyncError("微信界面不可用（需已登录且窗口可见）：%s" % e)
    if ops is None:
        raise TagSyncError("UIA 驱动不可用，无法读取标签成员")
    return ops


def _read_all_labels(ops, progress=None):
    """在 UI 门内：读所有标签定义 + 每个标签的成员 → {wxid: [tag_name]}。

    标签定义我们其实可以从 contact.db 纯库读（db.list_labels()），但为了让
    「删除了的标签」也和界面一致（库里会滞后），这里以界面标签行为准，
    库定义只作兜底：界面一个标签都展开不出时，退回库里的名字再试一遍。
    """
    # 先打开通讯录管理窗（label_rows 在窗口没开时返回空），再展开可能被记住折叠的
    # 「标签」分组——折叠时整组标签行都不在 UIA 树里，实测踩过。
    ui_names = []
    try:
        op = ops.open_manager()
        if op and op.get("ok"):
            ops.ensure_labels_open(win=op.get("win"))
            for r in (ops.label_rows(op.get("win")) or []):
                nm = (r.get("name") or "").strip()
                if nm and nm not in ui_names:
                    ui_names.append(nm)
    except Exception:                   # noqa: BLE001
        pass

    # 标签名清单：界面为准（含刚建、库里还没刷出来的），界面读不到再退回库定义
    names = list(ui_names)
    if not names:
        try:
            import db_gate
            names = [x["name"] for x in db_gate.get_db().list_labels()
                     if x.get("name")]
        except Exception:               # noqa: BLE001
            names = []

    mapping = {}
    details = []
    for idx, name in enumerate(names):
        try:
            r = ops.label_members(name)
        except Exception as e:          # noqa: BLE001
            details.append({"label": name, "ok": False,
                            "reason": "读取异常：%s" % e, "members": 0})
            continue
        ok = bool(r.get("ok"))
        reason = r.get("reason") or ""
        count = int(r.get("count") or 0)
        members = r.get("members") or []
        # no-list 且界面人数=0 → 空标签，正常；其他失败如实记
        if not ok and not (reason == "no-list" and count == 0):
            details.append({"label": name, "ok": False, "reason": reason,
                            "members": 0, "count": count})
            continue
        resolved = []
        skipped = []
        if members:
            try:
                rr = ops.resolve_members(members)
                resolved = rr.get("recipients") or []
                skipped = rr.get("skipped") or []
            except Exception as e:      # noqa: BLE001
                details.append({"label": name, "ok": False,
                                "reason": "解析成员失败：%s" % e,
                                "members": len(members)})
                continue
        for x in resolved:
            u = (x.get("username") or "").strip()
            if u:
                mapping.setdefault(u, []).append(name)
        details.append({
            "label": name,
            "ok": True,
            "count": count,
            "members": len(resolved),
            "skipped": len(skipped),
            "complete": bool(r.get("complete", True)),
            "reason": reason,
        })
        if progress:
            progress(idx + 1, len(names), name)
    return mapping, details, names


def sync(progress=None):
    """同步一次：UI 门内读全量标签成员 → 门外写缓存。

    返回 dict：ok / mapping_count / labels / tags_count / synced_at / details。
    UI 操作失败抛 TagSyncError（由接口层翻成人话）。
    """
    import ui_gate
    import monitor_store

    ops = _labels_ops()
    with ui_gate.hold("同步微信好友标签"):
        try:
            mapping, details, names = _read_all_labels(ops, progress=progress)
        finally:
            try:
                ops.back_to_chat()
            except Exception:           # noqa: BLE001
                pass

    # 门已释放，下面纯 DB 操作
    now = int(time.time())
    tags_count = monitor_store.replace_contact_tags(mapping, now=now)
    return {
        "ok": True,
        "labels": names,
        "labels_count": len(names),
        "mapping_count": len(mapping),   # 有多少个好友至少有一个标签
        "tags_count": tags_count,        # (好友,标签) 归属条数
        "skipped": sum(int(d.get("skipped") or 0) for d in details),
        "details": details,
        "synced_at": now,
    }
