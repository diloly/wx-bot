#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""图片素材库（本机私有，不入库）。

上传的图片落在 ``wechat_media/outbox/``，索引是同目录的 ``_index.json``。
这个目录已在 .gitignore 里：素材属于本机数据，绝不进公开仓库。

两条消费路径：
- 看板预览：复用已有 ``/media/<rel>`` 路由，item.url 直接可用；
- 发给微信：``resolve(id)`` 拿到绝对路径，由 wechat_ui 写进剪贴板 CF_DIB。

不信任任何外部传进来的路径：只认索引里的 id 或自己生成的 ``outbox/<文件名>``，
并且拼完再校验绝对路径没跑出 outbox 目录。
"""
import io
import json
import os
import time
import uuid

BASE = os.path.dirname(os.path.abspath(__file__))
MEDIA_DIR = os.path.join(BASE, "wechat_media")
OUTBOX = os.path.join(MEDIA_DIR, "outbox")
INDEX = os.path.join(OUTBOX, "_index.json")

# 扩展名 -> 落盘时统一用的后缀（jpg/jpeg 都存成 .jpg）
ALLOWED_EXT = {
    ".png": "png", ".jpg": "jpg", ".jpeg": "jpg",
    ".gif": "gif", ".webp": "webp", ".bmp": "bmp",
}
MAX_BYTES = 10 * 1024 * 1024
MAX_ITEMS = 200


def _ensure():
    os.makedirs(OUTBOX, exist_ok=True)


def _load():
    _ensure()
    if not os.path.isfile(INDEX):
        return []
    try:
        with open(INDEX, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:              # noqa: BLE001
        return []


def _save(items):
    """原子写，避免多线程上传时读到半截 JSON。"""
    _ensure()
    tmp = INDEX + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    os.replace(tmp, INDEX)


def _safe_name(name):
    return os.path.basename(str(name or "").replace("\\", "/"))[:120]


def save_upload(filename, data):
    """校验并落盘一张图，返回 ``(item, error)``；失败时 item 为 None。"""
    if not data:
        return None, "文件为空"
    if len(data) > MAX_BYTES:
        return None, "图片不能超过 10MB"
    ext = os.path.splitext(_safe_name(filename))[1].lower()
    if ext not in ALLOWED_EXT:
        return None, "只支持 png / jpg / gif / webp / bmp"
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            im.verify()            # 只验格式，不解码整图，防伪造扩展名
    except Exception:              # noqa: BLE001
        return None, "不是有效图片"
    _ensure()
    aid = uuid.uuid4().hex[:16]
    rel = "outbox/%s.%s" % (aid, ALLOWED_EXT[ext])
    full = os.path.join(MEDIA_DIR, rel.replace("/", os.sep))
    with open(full, "wb") as f:
        f.write(data)
    item = {
        "id": aid,
        "name": _safe_name(filename) or rel,
        "file": rel,
        "url": "/media/" + rel,
        "size": len(data),
        "created": int(time.time()),
    }
    items = _load()
    items.insert(0, item)
    _save(items[:MAX_ITEMS])
    return item, ""


def list_assets():
    """最新在前。索引里文件已被手工删掉的条目照样列出，由前端显示破图兜底。"""
    return _load()


def get_asset(aid):
    for it in _load():
        if it.get("id") == str(aid):
            return it
    return None


def resolve(aid_or_rel):
    """素材 id / 相对路径 -> 绝对路径；越界或文件不存在返回 None。"""
    s = str(aid_or_rel or "").strip()
    if not s:
        return None
    it = get_asset(s)
    rel = str((it or {}).get("file") or s).replace("\\", "/")
    if rel.startswith("outbox/"):
        rel = rel[len("outbox/"):]
    # 只准 outbox 下的单层文件名，不接受子目录、绝对路径、.. 穿越
    if not rel or "/" in rel or "\\" in rel or rel.startswith("..") or os.path.isabs(rel):
        return None
    base = os.path.abspath(OUTBOX)
    full = os.path.abspath(os.path.join(base, rel))
    if not full.startswith(base + os.sep):
        return None
    return full if os.path.isfile(full) else None


def delete_asset(aid):
    """删索引 + 删文件；文件删不掉不影响索引收敛。"""
    _ensure()
    items = _load()
    keep, removed = [], None
    for it in items:
        if it.get("id") == str(aid):
            removed = it
        else:
            keep.append(it)
    if not removed:
        return False
    _save(keep)
    full = os.path.join(MEDIA_DIR,
                        str(removed.get("file") or "").replace("/", os.sep))
    try:
        if os.path.isfile(full):
            os.remove(full)
    except Exception:              # noqa: BLE001
        pass
    return True
