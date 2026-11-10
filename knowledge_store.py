#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
知识库资料层：上传文件的落盘、文本化、启停、编辑、删除。
==================================================================
设计要点（为什么这么绕）：

1. **大模型看不到文件，只看到 prompt 里的文字**。所以每个上传文件都必须先
   变成纯文本；PDF 取文本层、docx 取段落、图片要靠 OCR 或人工补文字。
   拿不到文本的文件状态是 needs_text，绝不假装"已入库"。

2. **ai_client 只扫 knowledge/ 顶层的 .md / .txt**。本模块把提取出的正文
   存在 knowledge/_text/<id>.txt（下划线目录不会被扫描），启用时才在顶层
   生成一份 kb-*.md 镜像；停用/删除就把镜像删掉。这样检索层一行都不用改，
   而且按 mtime 自动重载——上传完下一条客户消息就生效，不用重启主进程。

3. **原文件、正文、元数据一律放 knowledge/，该目录已被 .gitignore 忽略**：
   公司产品资料与客户信息不进公开仓库。所有路径按 __file__ 相对定位。

4. 运行参数（mode / max_chars / top_k）写 knowledge/_config.json，
   ai_client 把它纳入目录签名，改配置同样无需重启。

只依赖标准库；pypdf / python-docx / pytesseract 都是可选增强，缺了就降级。
"""
import json
import logging
import os
import re
import threading
import time
import uuid

log = logging.getLogger("wechat_pc.knowledge")

BASE = os.path.dirname(os.path.abspath(__file__))
KB_DIR = os.path.join(BASE, "knowledge")
FILES_DIR = os.path.join(KB_DIR, "_files")     # 上传原文件
TEXT_DIR = os.path.join(KB_DIR, "_text")       # 提取/人工补的正文（唯一真源）
META_PATH = os.path.join(KB_DIR, "_meta.json")
CONFIG_PATH = os.path.join(KB_DIR, "_config.json")

_LOCK = threading.RLock()

MAX_UPLOAD_BYTES = 20 * 1024 * 1024           # 单文件 20 MB
MAX_TEXT_CHARS = 200000                        # 正文上限，防止把 prompt 撑爆
SECTION_CHARS = 1600                           # 单个检索单元的长度，长文自动切片

TEXT_EXTS = {".txt", ".md", ".markdown", ".csv", ".json", ".log", ".htm", ".html"}
PDF_EXTS = {".pdf"}
DOCX_EXTS = {".docx"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
ALLOWED_EXTS = TEXT_EXTS | PDF_EXTS | DOCX_EXTS | IMAGE_EXTS

DEFAULT_CONFIG = {"mode": "auto", "max_chars": 12000, "top_k": 3}
MODE_LABELS = {
    "auto": "自动（资料小就全量注入，超出预算改检索）",
    "full": "全量注入（不检索，全部给模型看）",
    "retrieve": "关键词检索（只注入命中的 top_k 段）",
}


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def _ensure_dirs():
    for d in (KB_DIR, FILES_DIR, TEXT_DIR):
        os.makedirs(d, exist_ok=True)


def _safe_name(name):
    """只取 basename，滤掉路径分隔符与 Windows 非法字符；中文文件名保留。"""
    name = os.path.basename(str(name or "").replace("\\", "/")).strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    return name.strip(" .")[:120] or "file"


def _slug(stem):
    """生成 md 镜像文件名用的短标识（只留中英数字与 -_）。"""
    s = re.sub(r"[^一-鿿0-9A-Za-z_-]+", "-", stem).strip("-")
    return s[:40] or "doc"


def _valid_id(kid):
    """id 只认 32 位小写 hex：它会被拼进文件路径，绝不能让 URL 传 ../ 进来。"""
    return bool(re.fullmatch(r"[0-9a-f]{32}", kid or ""))


def _text_path(kid):
    return os.path.join(TEXT_DIR, kid + ".txt")


def _md_path(item):
    stem = os.path.splitext(item["name"])[0]
    return os.path.join(KB_DIR, "kb-%s-%s.md" % (_slug(stem), item["id"][:6]))


def _decode(raw):
    for enc in ("utf-8-sig", "utf-8", "gb18030", "big5"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _strip_html(text):
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text)


# ---------------------------------------------------------------------------
# 元数据与配置
# ---------------------------------------------------------------------------
def _load_meta():
    try:
        with open(META_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("files"), list):
            return data
    except (OSError, ValueError):
        pass
    return {"files": []}


def _save_meta(data):
    _ensure_dirs()
    tmp = META_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, META_PATH)


def _find(meta, kid):
    for it in meta["files"]:
        if it.get("id") == kid:
            return it
    return None


def load_config():
    """知识库运行参数。文件不存在/写坏都用默认值，绝不让 AI 回复挂掉。"""
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            for k in DEFAULT_CONFIG:
                if k in raw:
                    cfg[k] = raw[k]
    except (OSError, ValueError):
        pass
    return normalise_config(cfg)


def normalise_config(patch):
    """校验并归一，防止前端传个离谱值把 prompt 撑爆。"""
    cur = load_config() if patch is None else None
    src = dict(cur or {})
    src.update({k: v for k, v in (patch or {}).items() if k in DEFAULT_CONFIG})
    mode = str(src.get("mode") or "auto")
    if mode not in MODE_LABELS:
        mode = "auto"
    try:
        max_chars = int(src.get("max_chars", 12000))
    except (TypeError, ValueError):
        max_chars = 12000
    try:
        top_k = int(src.get("top_k", 3))
    except (TypeError, ValueError):
        top_k = 3
    return {
        "mode": mode,
        "max_chars": max(1000, min(max_chars, 60000)),
        "top_k": max(1, min(top_k, 20)),
    }


def save_config(patch):
    cfg = normalise_config(patch)
    _ensure_dirs()
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)
    return cfg


def capabilities():
    """告诉前端哪些格式能自动提取；缺库时 UI 会提示人工补文本。"""
    return {
        "pdf": _has_pypdf(),
        "docx": _has_docx(),
        "ocr": _has_ocr(),
        "image_note": "图片需要本机装 Tesseract（含 chi_sim 中文包）才能自动 OCR；"
                      "当前回复走的是纯文本模型，就算不做 OCR 也不会把图片直接发给模型看。",
        "accepted": sorted(ALLOWED_EXTS),
        "max_upload_mb": MAX_UPLOAD_BYTES // (1024 * 1024),
    }


def _has_pypdf():
    try:
        import pypdf  # noqa: F401
        return True
    except ImportError:
        try:
            import PyPDF2  # noqa: F401
            return True
        except ImportError:
            return False


def _has_docx():
    try:
        import docx  # noqa: F401
        return True
    except ImportError:
        return False


def _has_ocr():
    try:
        import pytesseract  # noqa: F401
        from PIL import Image  # noqa: F401
        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# 文本化：上传文件 → 纯文本
# ---------------------------------------------------------------------------
def extract_text(path, ext):
    """返回 (text, extractor, note)。text 为空表示这份资料还不能给模型用。"""
    if ext in TEXT_EXTS:
        try:
            with open(path, "rb") as f:
                text = _decode(f.read(MAX_UPLOAD_BYTES + 1))
        except OSError as e:
            return "", "直接读取", "读取失败：%s" % e
        if ext in (".htm", ".html"):
            text = _strip_html(text)
        return text.strip(), "直接读取", ""

    if ext in PDF_EXTS:
        if not _has_pypdf():
            return "", "pypdf", "没装 pypdf：pip install pypdf 后重新上传，或在下面直接粘贴文字。"
        try:
            try:
                from pypdf import PdfReader
            except ImportError:
                from PyPDF2 import PdfReader
            reader = PdfReader(path)
            pages = reader.pages[:300]        # 超长 PDF 只读前 300 页，防止卡死
            text = "\n\n".join((p.extract_text() or "") for p in pages)
            text = text.strip()
            if not text:
                return "", "pypdf", "这个 PDF 没有文本层（多半是扫描件/纯图片），"
                "模型看不到内容。请在下面粘贴文字，或先做 OCR。"
            return text, "pypdf 提取文本", ""
        except Exception as e:                # noqa: BLE001
            return "", "pypdf", "PDF 解析失败：%s。可在下面直接粘贴文字。" % e

    if ext in DOCX_EXTS:
        if not _has_docx():
            return "", "python-docx", "没装 python-docx：pip install python-docx 后重新上传，或粘贴文字。"
        try:
            from docx import Document
            doc = Document(path)
            parts = [p.text for p in doc.paragraphs if p.text.strip()]
            for table in doc.tables:
                for row in table.rows:
                    parts.append("\t".join(c.text for c in row.cells))
            text = "\n".join(parts).strip()
            return (text, "python-docx 提取文本", "") if text else (
                "", "python-docx", "文档里没抽到文字，请在下面粘贴。")
        except Exception as e:                # noqa: BLE001
            return "", "python-docx", "docx 解析失败：%s。可在下面粘贴文字。" % e

    if ext in IMAGE_EXTS:
        if _has_ocr():
            try:
                import pytesseract
                from PIL import Image
                text = pytesseract.image_to_string(Image.open(path), lang="chi_sim+eng").strip()
                if text:
                    return text, "OCR（Tesseract）", ""
            except Exception as e:            # noqa: BLE001
                return "", "OCR", "OCR 失败（%s）。多半是没装 Tesseract 或中文语言包，"
                "请在下面粘贴图片里的文字。" % e
        return "", "待补文本", "图片是像素，纯文本模型看不见。请粘贴图里的文字/说明，"
        "或安装 Tesseract + chi_sim 后重新上传做 OCR。"

    return "", "待补文本", "暂不支持自动提取这种格式，请在下面粘贴文字。"


def _split_chunks(text, limit=SECTION_CHARS):
    """把长正文按空行切成不超过 limit 字的片段，尽量不切断句子。"""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks, buf = [], ""
    for p in paras:
        while len(p) > limit:                # 单段超长：硬切，避免一个单元塞满预算
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.append(p[:limit])
            p = p[limit:]
        if buf and len(buf) + len(p) + 2 > limit:
            chunks.append(buf)
            buf = p
        else:
            buf = (buf + "\n\n" + p).strip() if buf else p
    if buf:
        chunks.append(buf)
    return chunks


def _to_md(text):
    """正文 → 顶层 .md 镜像的内容。长文用 ## 切段，ai_client 会切成多个检索单元。"""
    chunks = _split_chunks(text)
    if len(chunks) <= 1:
        return (chunks[0] if chunks else "").strip()
    return "\n\n".join("## 片段 %d\n\n%s" % (i + 1, c) for i, c in enumerate(chunks))


def _read_text(kid):
    try:
        with open(_text_path(kid), "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def _write_text(kid, text):
    _ensure_dirs()
    with open(_text_path(kid), "w", encoding="utf-8") as f:
        f.write(text)


def _sync_md(item):
    """按 enabled / 是否有文本，生成或移除 ai_client 能扫到的 .md 镜像。"""
    path = _md_path(item)
    if item.get("enabled") and item.get("chars", 0) > 0:
        text = _read_text(item["id"])
        if text.strip():
            # 先写 .tmp 再原子替换：ai_client 的轮询可能在写文件的半途扫到目录，
            # 直接 open(path,"w") 会让它读到半截内容。.tmp 不在扫描后缀里。
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(_to_md(text))
            os.replace(tmp, path)
            item["md"] = os.path.basename(path)
            return
    if os.path.isfile(path):
        try:
            os.remove(path)
        except OSError:
            pass
    item["md"] = ""


def _status(item):
    if item.get("chars", 0) <= 0:
        return "needs_text"
    return "ready" if item.get("enabled") else "disabled"


def _public(item):
    status = _status(item)
    return {
        "id": item["id"],
        "name": item["name"],
        "ext": item.get("ext", ""),
        "size": item.get("size", 0),
        "uploaded": item.get("uploaded", 0),
        "chars": item.get("chars", 0),
        "enabled": bool(item.get("enabled")),
        "status": status,
        "extractor": item.get("extractor", ""),
        "note": item.get("note", ""),
        "manual": bool(item.get("manual")),
        "md": item.get("md", ""),
    }


# ---------------------------------------------------------------------------
# 对外操作
# ---------------------------------------------------------------------------
def save_upload(filename, raw):
    """保存一个上传文件并立即文本化。返回 (public_item, error)。"""
    name = _safe_name(filename)
    ext = os.path.splitext(name)[1].lower()
    if ext not in ALLOWED_EXTS:
        return None, "不支持的格式 %s（允许：%s）" % (ext or "无扩展名", ", ".join(sorted(ALLOWED_EXTS)))
    if not raw:
        return None, "文件是空的"
    if len(raw) > MAX_UPLOAD_BYTES:
        return None, "文件超过 %d MB" % (MAX_UPLOAD_BYTES // (1024 * 1024))

    _ensure_dirs()
    kid = uuid.uuid4().hex
    with _LOCK:
        meta = _load_meta()
        # 同名 + 同大小视为重复上传，直接覆盖旧记录，避免 md 镜像越堆越多
        for old in list(meta["files"]):
            if old["name"] == name and old.get("size") == len(raw):
                delete(old["id"])
                meta = _load_meta()
                break
        path = os.path.join(FILES_DIR, kid + ext)
        with open(path, "wb") as f:
            f.write(raw)

        text, extractor, note = extract_text(path, ext)
        text = text[:MAX_TEXT_CHARS]
        _write_text(kid, text)
        item = {
            "id": kid,
            "name": name,
            "ext": ext,
            "size": len(raw),
            "uploaded": int(time.time()),
            "chars": len(text),
            "enabled": bool(text.strip()),     # 拿不到文本就默认不启用
            "extractor": extractor,
            "note": note,
            "manual": False,
            "md": "",
        }
        _sync_md(item)
        meta["files"].append(item)
        _save_meta(meta)
        return _public(item), None


def list_items():
    with _LOCK:
        items = [_public(it) for it in _load_meta()["files"]]
    items.sort(key=lambda x: -x["uploaded"])
    return items


def get_text(kid):
    if not _valid_id(kid):
        return None
    with _LOCK:
        it = _find(_load_meta(), kid)
        if not it:
            return None
        return {"text": _read_text(kid), "item": _public(it)}


def set_text(kid, text):
    """人工补文本/编辑正文：写完立刻重建 .md 镜像，mtime 变化让检索层自动重载。"""
    if not _valid_id(kid):
        return None, "资料 id 不合法"
    text = (text or "").strip()
    if len(text) > MAX_TEXT_CHARS:
        return None, "正文超过 %d 字上限" % MAX_TEXT_CHARS
    with _LOCK:
        meta = _load_meta()
        it = _find(meta, kid)
        if not it:
            return None, "资料不存在"
        _write_text(kid, text)
        it["chars"] = len(text)
        it["manual"] = True
        it["extractor"] = "人工补文本" if text else it.get("extractor", "")
        if text:
            it["note"] = ""
            it["enabled"] = True
        _sync_md(it)
        _save_meta(meta)
        return _public(it), None


def set_enabled(kid, enabled):
    if not _valid_id(kid):
        return None, "资料 id 不合法"
    with _LOCK:
        meta = _load_meta()
        it = _find(meta, kid)
        if not it:
            return None, "资料不存在"
        if enabled and it.get("chars", 0) <= 0:
            return None, "这份资料还没文字，先补文本再启用"
        it["enabled"] = bool(enabled)
        _sync_md(it)
        _save_meta(meta)
        return _public(it), None


def delete(kid):
    if not _valid_id(kid):
        return False
    with _LOCK:
        meta = _load_meta()
        it = _find(meta, kid)
        if not it:
            return False
        for p in (
            os.path.join(FILES_DIR, kid + (it.get("ext") or "")),
            _text_path(kid),
            _md_path(it),
        ):
            try:
                if os.path.isfile(p):
                    os.remove(p)
            except OSError as e:
                log.warning("删除 %s 失败: %s", os.path.basename(p), e)
        meta["files"] = [x for x in meta["files"] if x.get("id") != kid]
        _save_meta(meta)
        return True


def original_path(kid):
    if not _valid_id(kid):
        return None, None
    with _LOCK:
        it = _find(_load_meta(), kid)
        if not it:
            return None, None
        path = os.path.join(FILES_DIR, kid + (it.get("ext") or ""))
        if not os.path.isfile(path):
            return None, None
        return path, it["name"]
