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

成员行 → wxid **不用 wechatauto 的 resolve_members**：那是给「安全群发」设计的，
要求显示名精确等于微信库字段、重名一律拒绝，拿来做标签缓存会确定性漏人（昵称
带空格、备注名≠昵称、多标签拼接、重名）。这里改用本地通讯录的 remark/name/nick
做精确匹配（见 _local_resolve），匹配不上才 skipped、且如实列出名字不静默丢。
标签行自带人数 ``count``，和读到数对不上时 complete=False——不假装同步全了。
空标签（no-list 且 count=0）算正常空结果。

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


def _local_resolve(members, local_contacts):
    """成员行 → wxid，用**本地通讯录**做精确匹配（不靠 wechatauto 的猜名字）。

    wechatauto 的 resolve_members 为「安全群发」设计：成员行的名字必须精确等于
    微信库 nick_name/remark，重名一律拒绝。拿来做标签缓存太严——昵称带空格、
    备注名≠昵称、多标签拼接都会确定性漏人（不是偶发）。

    这里改用 wx_recv_bridge 已同步好的 monitor_store.contacts（conv_id/name/nick/
    remark），对成员行的每个可用片段做**精确**匹配，优先级：
        remark 精确 > name 精确 > nick 精确
    精确匹配不上才算 skipped（仍然不猜——重名/模糊一律不认，避免打错标签）。

    返回 (resolved=[{username,display}], skipped=[{display,reason}])。
    """
    # 建三个索引：值 -> [wxid]（同名多个都收着，下面按唯一性判定）
    idx = {"remark": {}, "name": {}, "nick": {}}
    for c in local_contacts:
        u = (c.get("conv_id") or "").strip()
        if not u:
            continue
        for field in idx:
            v = (c.get(field) or "").strip()
            if v:
                idx[field].setdefault(v, set()).add(u)

    resolved, skipped = [], []
    for m in members or []:
        raw = (m.get("raw") or "").strip()
        # 成员行是「昵称  备注  标签」用空格拼的。先试结构化的 remark/nick/display，
        # 再补 raw 按空格拆出的各段——精确匹配天然不会把「标签」误当名字（标签名
        # 一般不在 exact 索引里），带空格昵称的整段也能命中。
        cands = []
        for key in ("remark", "nick", "display"):
            v = (m.get(key) or "").strip()
            if v and v not in cands:
                cands.append(v)
        for v in raw.split(" "):
            v = v.strip()
            if v and v not in cands:
                cands.append(v)

        hit = None
        for field in ("remark", "name", "nick"):
            for v in cands:
                users = idx[field].get(v) or set()
                if len(users) == 1:
                    hit = (next(iter(users)), v)
                    break
            if hit:
                break
        if hit:
            resolved.append({"username": hit[0], "display": hit[1]})
        else:
            skipped.append({"display": raw or (m.get("display") or ""),
                            "reason": "本地通讯录无精确匹配"})
    return resolved, skipped


def _read_all_labels(ops, progress=None, local_contacts=None):
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

    if local_contacts is None:
        try:
            import monitor_store
            local_contacts = monitor_store.list_contacts()
        except Exception:               # noqa: BLE001
            local_contacts = []

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
        # 空标签（界面人数=0）的各种表现都算正常：no-list / not-selected（右侧没
        # 切过去，往往就是因为没人）。其余失败才如实记。
        if not ok and not (count == 0 and reason in ("no-list", "not-selected")):
            details.append({"label": name, "ok": False, "reason": reason,
                            "members": 0, "count": count})
            continue
        resolved, skipped = _local_resolve(members, local_contacts)
        for x in resolved:
            u = (x.get("username") or "").strip()
            if u:
                buckets = mapping.setdefault(u, [])
                if name not in buckets:     # 同一标签去重（多次同步不重复堆）
                    buckets.append(name)
        details.append({
            "label": name,
            "ok": True,
            "count": count,
            "members": len(resolved),
            "skipped": len(skipped),
            "skipped_names": [x.get("display") for x in skipped],
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
    import monitor_store
    local_contacts = monitor_store.list_contacts()   # UI 门之外读本地通讯录

    with ui_gate.hold("同步微信好友标签"):
        try:
            mapping, details, names = _read_all_labels(
                ops, progress=progress, local_contacts=local_contacts)
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
