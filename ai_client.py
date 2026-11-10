#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI 外发内容生成（OpenAI 兼容接口，支持本地 Ollama / DeepSeek / OpenAI / 通义 等切换）
==================================================================================
- 只负责"生成一条发给客户的微信文案"，不碰发送；发送由 wechat_ui.py 的键盘流程负责。
- **AI 配置统一放同目录 ai_secrets.json**（provider/model/system_prompt/temperature/
  max_tokens/base_url/api_key 全在这一个文件；含 api_key，不要外发/截图/提交 git）。
  config.json 不再放 ai 块（2026-10-15 合并；旧配置代码仍兼容兜底）。
- 本地 Ollama 默认 base_url=http://localhost:11434/v1；custom 的 base_url 须写全
  （含 /v1 路径，代码不做拼接）。
- 所有主流服务都兼容 OpenAI 的 /v1/chat/completions，所以一套代码走天下。
- **销售化系统提示词（2026-10-15）**：默认 prompt 是「销售顾问」人设，含
  ① 身份纪律（被问到是不是 AI 必须坦然承认，没问不主动提）
  ② 微信聊天风格 ③ 产品知识注入 ④ 输出纪律。
  ai_secrets.json 显式写 system_prompt 可完全接管 prompt（那样就不注入知识库）。
- **知识库（2026-11-10 改造）**：**模型不会读文件、没有记忆**，唯一通道是每次
  请求把资料原文拼进 system prompt。所以"精准"取决于检索召回，与模型无关。
  knowledge/ 顶层的 .md / .txt 按 ## 切成检索单元，按目录 mtime 自动重载；
  mode=auto（默认）在资料总量 ≤ max_chars 时**全量注入**（小资料库最稳、零漏召回），
  超预算才退回中文 bigram 关键词检索 top_k。
  上传文件的落盘与文本化见 knowledge_store.py；运行参数在 knowledge/_config.json。
  详见 knowledge/_说明.md。
"""
import json
import logging
import os
import re
import time

import requests

log = logging.getLogger("wechat_pc.ai")

# ==================== 销售化系统提示词 ====================
# 默认人设：客户激活型销售。{persona}/{product}/{knowledge} 由 build_system() 填充。
# ai_secrets.json 里显式写 system_prompt 可整体覆盖本模板（完全自管 prompt 时用）。
SALES_SYSTEM_TEMPLATE = """你是{persona}，{company}的在线顾问，负责核心产品「{product}」。公司做的是{positioning}，你正在微信上和客户一对一沟通。
你的任务：激活客户——让沉默的客户重新愿意聊，让在用的客户愿意续费/扩购/转介绍。

【定位纪律——身份别搞错】
- 我们卖的是 **SaaS 平台/工具**，不是设计服务商：卖的是让设计师更快出方案的工具，
  不接设计项目、不代做方案、不代出图。客户问"能不能帮我把这个项目做了/帮我出套图/接个单"，
  统一回"我们是给设计师用的工具，具体能不能覆盖您的需求，我让同事看下再回您"——不接活、不报工期。
- 一句话定位：{positioning}，覆盖照明、建筑、室内、景观设计。

【身份纪律——合规红线，问必答】
- 你是 AI 销售助手。被问到"你是机器人吗/是AI吗/是不是真人"时**必须坦然承认**，
  不冒充、不遮掩、不转移话题。示例："对，我是平台的 AI 销售助手，24 小时在线的，
  产品问题随时问我。"AI 生成内容须可识别身份，瞒报是红线。
- 客户没问就**不要主动提**自己是 AI——被问到了才坦然承认，不默认声明身份。
- 承认身份后立刻拉回价值：响应快、资料准、不用等销售排期——把 AI 身份说成优势，
  不是减分项。不假装自己在外跑客户、开会、应酬（不编造真人状态）。
- 不提具体模型名、公司技术细节。

【聊天风格】
- 自然、专业、亲和：像一个靠谱的在线顾问；短句、口语、可用语气词；
  禁"尊敬的客户"、禁营销腔、禁长篇大论。
- 默认不超过3句话；客户明确问了需要展开的问题才展开，且不超过5句。
- 不群发感：不重复客户原话、不背诵产品介绍，像一对一聊天。
- 客户冷淡或明显没需求：不硬推，轻维护关系，留个下次聊的钩子。

【产品知识】
以下是公司产品资料，只在与客户问题相关时自然引用，绝不倒背如流：
{knowledge}
- 资料里没有答案的问题（价格细节、合同、上线时间、定制），绝不编造，
  统一说"这个我确认一下再回复你"。
- 不替公司承诺折扣、赠品、效果；不贬低竞品。

【输出纪律】
只输出微信消息正文。不要引号、不要解释、不要"好的这是"开场白、不要自称前缀。"""


# ==================== 知识库（prompt 注入，不是模型记忆） ====================
# 关键认知：模型不读本地文件、没有跨次记忆。它"知道"的全部内容，就是这一次
# /chat/completions 请求里 system 消息中的那几段原文。所以精准度 = 检索召回率。
_KB_CACHE = {"sig": None, "units": []}   # 按目录 mtime 签名缓存，改文件下条消息生效

DEFAULT_KNOWLEDGE = {"dir": "knowledge", "mode": "auto", "max_chars": 12000, "top_k": 3}

# 中文虚词/停用 bigram：出现这些的二元组不参与打分（降低"的是""可以"的噪音）
_STOP_BIGRAMS = {
    "的是", "了是", "是不", "是不", "可以", "以一", "一个", "什么", "怎么", "这样",
    "那样", "现在", "就是", "还是", "不是", "没有", "我们", "你们", "他们", "这个",
    "那个", "一下", "如果", "因为", "所以", "但是", "而且", "或者", "已经", "应该",
    "需要", "觉得", "知道", "看看", "帮我", "您好", "你好", "谢谢", "请问",
}


def _bigrams(text):
    """中文二元切词：只留中英数字，切相邻两字，去停用。无分词依赖，够 v1 用。"""
    t = re.sub(r"[^一-鿿0-9a-zA-Z]", "", text or "")
    out = set()
    for i in range(len(t) - 1):
        g = t[i:i + 2]
        if g not in _STOP_BIGRAMS:
            out.add(g)
    return out


def _split_sections(path):
    """一个 md/txt 文件按 `## 标题` 切成检索单元；没有 ## 的整文件算一个单元。"""
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError):
        return []
    parts = re.split(r"(?m)^##\s+", text)
    units = []
    if len(parts) == 1:                    # 无 ## 小节：整文件一个单元
        title = os.path.splitext(os.path.basename(path))[0]
        body = parts[0].strip()
        if body:
            units.append({"file": os.path.basename(path), "title": title, "text": body})
        return units
    head = parts[0].strip()                # ## 之前的内容（通常是 # 文件标题/说明）
    if head:
        units.append({"file": os.path.basename(path),
                      "title": os.path.splitext(os.path.basename(path))[0],
                      "text": head})
    for p in parts[1:]:
        nl = p.find("\n")
        title = (p[:nl] if nl > 0 else p).strip("# \t")
        body = (p[nl:] if nl > 0 else "").strip()
        if body:
            units.append({"file": os.path.basename(path), "title": title, "text": body})
    return units


def _dir_signature(d):
    """顶层 .md/.txt 的 (name, mtime_ns, size)；外加运行配置的 mtime。

    把 _config.json 纳入签名：看板改了 mode/max_chars 之后，auto_reply 不用重启，
    下一条消息就能按新参数注入。mtime_ns 而非秒级 mtime —— Windows 上同一秒内
    连续上传/改配置，秒级签名会分不出变化。
    """
    sig = []
    try:
        for fn in sorted(os.listdir(d)):
            if fn.startswith("_") or not fn.lower().endswith((".md", ".txt")):
                continue                    # _ 开头是说明文件/元数据，不进检索库
            fp = os.path.join(d, fn)
            if not os.path.isfile(fp):
                continue
            st = os.stat(fp)
            sig.append((fn, st.st_mtime_ns, st.st_size))
        cp = os.path.join(d, "_config.json")
        if os.path.isfile(cp):
            st = os.stat(cp)
            sig.append(("_config.json", st.st_mtime_ns, st.st_size))
    except OSError:
        return None
    return tuple(sig)


def load_knowledge(dirname="knowledge"):
    """加载知识库单元（带 mtime 缓存）。返回 [{file,title,text}, ...]；目录不存在返回 []。"""
    d = dirname if os.path.isabs(dirname) else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), dirname)
    sig = _dir_signature(d)
    if sig is None:
        return []
    if _KB_CACHE["sig"] == sig:
        return _KB_CACHE["units"]
    units = []
    for fn, _mtime, _size in sig:
        if fn.lower().endswith((".md", ".txt")):
            units.extend(_split_sections(os.path.join(d, fn)))
    _KB_CACHE["sig"] = sig
    _KB_CACHE["units"] = units
    return units


def load_kb_config(cfg):
    """config.json 的 knowledge 块 + knowledge/_config.json（看板可改，后者优先）。"""
    kcfg = dict(DEFAULT_KNOWLEDGE)
    kcfg.update({k: v for k, v in (cfg.get("knowledge") or {}).items()
                 if k in DEFAULT_KNOWLEDGE})
    try:
        import knowledge_store
        kcfg.update(knowledge_store.load_config())
    except Exception as e:                 # noqa: BLE001
        log.debug("知识库运行配置读取失败，用 config.json 默认值: %s", e)
    return kcfg


def retrieve_scored(units, query, top_k=3):
    """按 query 与每个单元的重合度打分，返回 [(score, unit)] 降序。

    修掉旧实现的权重 bug：以前写 `tg = list(title_bigrams)*3 + list(body_bigrams)`
    再 `set(tg)`，重复的标题二元组被去重吃光，标题 x3 权重其实从未生效。
    现在显式区分「命中标题=3 分 / 只命中正文=1 分」。
    注意这仍是**字面匹配**：客户说"多少钱"、资料写"定价/报价"，一个字都对不上，
    就召回不到 —— 这正是小资料库默认改成全量注入的原因。
    """
    qg = _bigrams(query)
    if not qg or not units:
        return []
    scored = []
    for u in units:
        tg = _bigrams(u["title"])
        bg = _bigrams(u["text"])
        hit = qg & (tg | bg)
        if not hit:
            continue
        got = sum(3.0 if g in tg else 1.0 for g in hit)
        scored.append((round(got / (len(qg) * 3.0), 4), u))
    scored.sort(key=lambda x: -x[0])
    return scored[:max(1, int(top_k))]


def retrieve(units, query, top_k=3):
    """兼容旧调用：只要单元列表，不要分数。全不相关返回 []（不硬塞）。"""
    return [u for _s, u in retrieve_scored(units, query, top_k)]


def _unit_brief(score, u):
    return {
        "file": os.path.splitext(u["file"])[0],
        "title": u["title"],
        "chars": len(u["text"]),
        "score": score,
        "preview": u["text"][:140],
    }


def select_units(units, query, kcfg):
    """决定这一次请求给模型看哪些资料，并守住 max_chars 预算。

    mode:
      full      —— 不问相关度，全部单元按文件顺序注入
      retrieve  —— 只注入关键词命中的 top_k 个单元
      auto(默认)—— 资料总量 ≤ 预算就 full（小资料库零漏召回），超了自动 retrieve
    返回 (hits, info)，info 供看板「检索自检」把模型视野可视化。
    """
    total_chars = sum(len(u["text"] or "") for u in units)
    try:
        budget = max(1000, int(kcfg.get("max_chars", 12000)))
    except (TypeError, ValueError):
        budget = 12000
    mode = str(kcfg.get("mode") or "auto")
    if mode not in ("auto", "full", "retrieve"):
        mode = "auto"
    effective = ("full" if mode == "full" or (mode == "auto" and total_chars <= budget)
                 else "retrieve")
    if effective == "full":
        scored = [(1.0, u) for u in units]
    else:
        scored = retrieve_scored(units, query, kcfg.get("top_k", 3))

    injected, used = [], 0
    for s, u in scored:
        if used >= budget:
            break
        injected.append((s, u))
        used += len(u["text"] or "")
    hits = [u for _s, u in injected]
    info = {
        "mode": mode,
        "effective": effective,
        "budget": budget,
        "total_units": len(units),
        "total_chars": total_chars,
        "injected_chars": used,
        "truncated": used > budget,
        "matched": [_unit_brief(s, u) for s, u in scored],
        "injected": [_unit_brief(s, u) for s, u in injected],
    }
    return hits, info


def diagnose(query, cfg=None):
    """给看板「检索自检」用：原样跑一遍选段逻辑，回答「这次模型到底看到了什么」。"""
    kcfg = load_kb_config(cfg or {})
    units = load_knowledge(kcfg.get("dir", "knowledge"))
    _hits, info = select_units(units, query, kcfg)
    info["query"] = query
    info["config"] = kcfg
    return info


def build_system(ai_cfg, knowledge_units, max_chars=None):
    """组装销售系统提示词：人设 + 定位 + 产品知识小节。
    ai_cfg 可覆写 persona/company/product/positioning（config.json 的 ai 块）。"""
    persona = (ai_cfg.get("persona") or "小光").strip()
    company = (ai_cfg.get("company") or "中照金智（广州）数据科技").strip()
    product = (ai_cfg.get("product") or "AI照明工作站（A站）").strip()
    positioning = (ai_cfg.get("positioning") or "为设计提效的 AI SaaS 平台").strip()
    if knowledge_units:
        blocks = ["〔资料：%s / %s〕\n%s" %
                  (os.path.splitext(u["file"])[0], u["title"], u["text"])
                  for u in knowledge_units]
        knowledge = "\n\n".join(blocks)
        # 单元级预算之外再做一次字符级硬截：单个超长单元也不许把 prompt 撑爆
        if max_chars and len(knowledge) > max_chars:
            knowledge = knowledge[:max_chars] + "\n…（资料超出预算，后面已截断）"
    else:
        knowledge = "（暂无相关资料。客户问到产品细节就回答'我确认一下再回复你'，不要编造。）"
    return SALES_SYSTEM_TEMPLATE.format(
        persona=persona, company=company, product=product,
        positioning=positioning, knowledge=knowledge)

# provider -> 默认 base_url / api_key。所有 AI 覆盖项（provider/model/prompt/base_url/
# api_key）统一写 ai_secrets.json，优先级最高。"custom" 用于自建/中转端点：base_url
# 必须在 secrets 里给全（含 /v1，代码不拼）。
_PRESETS = {
    "ollama":   {"base_url": "http://localhost:11434/v1", "api_key": ""},
    "deepseek": {"base_url": "https://api.deepseek.com/v1", "api_key": ""},
    "openai":   {"base_url": "https://api.openai.com/v1",  "api_key": ""},
    "qwen":     {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "api_key": ""},
    "custom":   {"base_url": "", "api_key": ""},
}

# 保密配置文件：与 ai_client.py 同目录，按 __file__ 定位（不受运行时的 cwd 影响）。
_SECRETS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ai_secrets.json")


def _load_secrets():
    """读取 ai_secrets.json 里的全部 AI 配置（provider/model/system_prompt/
    temperature/max_tokens/base_url/api_key；下划线开头的说明字段忽略）。
    文件不存在 = 返回 {}，由上层按 config.ai / 环境变量 / provider 预设继续兜底。
    任何日志/报错都不得打印 api_key——只报"哪个键有问题"。"""
    try:
        with open(_SECRETS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        raise RuntimeError(
            "ai_secrets.json 不是合法 JSON：%s。请检查逗号/引号；该文件含密钥，不要外发。" % e)
    if not isinstance(data, dict):
        raise RuntimeError("ai_secrets.json 顶层必须是 JSON 对象（{...}）")
    out = {}
    for k, v in data.items():
        if k.startswith("_"):
            continue
        # 字符串键（provider/model/prompt/base_url/api_key）与数字键
        # （temperature/max_tokens）都合法；bool 是 int 子类，需显式排除。
        if isinstance(v, bool) or not isinstance(v, (str, int, float)):
            raise RuntimeError(
                "ai_secrets.json 的 %r 必须是字符串或数字（当前是 %s）" % (k, type(v).__name__))
        out[k] = v
    return out


def _pick(secrets, key, cfg_value, default):
    """按 优先级 ai_secrets.json > config.ai > default 取一个配置项；
    空串/None 都视为"没配"，继续往下兜底。"""
    v = secrets.get(key)
    if v is None or v == "":
        v = cfg_value
    return default if v is None or v == "" else v


def _number(key, v, cast):
    """把 temperature/max_tokens 归一成数字；写错时点名报错（值非保密，可打印）。"""
    try:
        return cast(v)
    except (TypeError, ValueError):
        raise RuntimeError(
            "ai_secrets.json 的 %r 必须是数字（当前值：%r）" % (key, v))


def _resolve(cfg):
    """解析 AI 参数。优先级：ai_secrets.json > 环境变量 > config.ai > provider 预设。
    AI 配置（provider/model/prompt/温度/base_url/api_key）自 2026-10-15 起统一放
    ai_secrets.json；config.json 的 ai 块仅作旧版兼容兜底。"""
    secrets = _load_secrets()
    ai = cfg.get("ai") or {}
    provider = str(_pick(secrets, "provider", ai.get("provider"), "ollama")).lower()
    preset = _PRESETS.get(provider, {})
    # 优先级：ai_secrets.json > 环境变量 > config.json > provider 预设
    base_url = (
        secrets.get("base_url")
        or os.environ.get("WECHAT_AI_BASE_URL")
        or ai.get("base_url")
        or preset.get("base_url")
        or ""
    )
    api_key = (
        secrets.get("api_key")
        or os.environ.get("WECHAT_AI_API_KEY")
        or ai.get("api_key")
        or preset.get("api_key")
        or ""
    )
    if not base_url:
        raise RuntimeError(
            "未解析到 base_url：provider=%r 没有内置默认地址。"
            "请把 base_url（和 api_key）填进同目录的 ai_secrets.json，"
            "或把 provider 改成 ollama/deepseek/openai/qwen 之一。" % provider)
    # Ollama 默认用 provider 名当模型（ollama 的 model 即本地已 pull 的标签）；
    # 云端默认给一个通用占位，建议在 ai_secrets.json 里写明确模型名。
    default_model = provider if provider == "ollama" else "gpt-4o-mini"
    model = _pick(secrets, "model", ai.get("model"), default_model)
    # system_prompt：secrets/config 显式给了就整体用；留空 = 用内置销售模板
    # （模板的人设/产品名走 config.ai.persona / config.ai.product 覆写）。
    system_prompt = _pick(secrets, "system_prompt", ai.get("system_prompt"), "")
    temperature = _number("temperature", _pick(secrets, "temperature", ai.get("temperature"), 0.7), float)
    max_tokens = _number("max_tokens", _pick(secrets, "max_tokens", ai.get("max_tokens"), 300), int)
    return base_url, api_key, model, system_prompt, temperature, max_tokens, provider


def build_user_prompt(contact, intent):
    name = contact.get("name", "")
    tags = ",".join(contact.get("tags", [])) or "无"
    note = contact.get("note", "")
    lines = [f"客户称呼：{name}", f"标签：{tags}"]
    if note:
        lines.append(f"背景备注：{note}")
    lines.append(f"本次沟通意图：{intent}")
    lines.append("请直接生成一条微信消息正文。")
    return "\n".join(lines)


def _clean(text):
    """去掉模型可能多包的引号/前缀，拿到纯正文。"""
    text = (text or "").strip()
    for q in ('"', "'", "“", "”", "‘", "’"):
        if len(text) >= 2 and text.startswith(q) and text.endswith(q):
            text = text[1:-1].strip()
    # 去掉 "消息正文：" / "微信消息：" 之类前缀
    for prefix in ("消息正文：", "消息正文:", "微信消息：", "微信消息:"):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
    return text


def _extract_content(data):
    """从 chat/completions 响应里取正文，返回 (text, 失败原因)。

    为什么要单独抽出来（2026-10-21 用户反馈「经常 AI 返回空内容，但中转站后台
    显示成功」）：HTTP 200 只代表网关把请求走完了，**不代表模型吐了字**。中转站
    在限流/排队/模型空转时会返回 200 + `content: ""` 或 `null`。原来直接
    `data["choices"][0]["message"]["content"]`，取到空串就判失败，连原因都看不出来。
    这里顺带兼容多模态分段写法（content 是 [{type:text,text:...}] 的列表）。
    """
    choices = data.get("choices") or []
    if not choices:
        return "", "choices 为空"
    msg = choices[0].get("message") or {}
    c = msg.get("content")
    if c is None:
        return "", "content 为 null"
    if isinstance(c, list):                 # 多模态分段
        c = "".join(x.get("text", "") for x in c if isinstance(x, dict))
    c = (c or "").strip()
    return c, ("" if c else "content 为空串")


def _finish_reason(data):
    """取 finish_reason（取不到时给 '?'，便于日志里看出是网关没给）。"""
    choices = data.get("choices") or []
    if not choices:
        return "?"
    return choices[0].get("finish_reason") or "?"


def generate(cfg, contact, intent, extra_context="", timeout=60):
    """调用大模型生成一条发给客户的微信文案，返回清洗后的纯文本。

    extra_context：触发本次回复的客户原话（自动回复时传入）。它参与知识库检索，
    决定注入哪些产品资料小节——客户问什么才给 AI 看什么，不相关的资料不塞。
    """
    base_url, api_key, model, system_prompt, temperature, max_tokens, provider = _resolve(cfg)
    ai_cfg = cfg.get("ai") or {}
    if not system_prompt:
        # 走内置销售模板：把选中的产品资料原文拼进 system（模型唯一的知识来源）
        kcfg = load_kb_config(cfg)
        units = load_knowledge(kcfg.get("dir", "knowledge"))
        query = " ".join([
            intent or "", extra_context or "",
            contact.get("name", ""), ",".join(contact.get("tags", []) or []),
            contact.get("note", "") or "",
        ])
        hits, info = select_units(units, query, kcfg)
        log.info("知识库注入: mode=%s/%s %d/%d 段 %d/%d 字",
                 info["mode"], info["effective"], len(hits),
                 info["total_units"], info["injected_chars"], info["total_chars"])
        system_prompt = build_system(ai_cfg, hits, kcfg.get("max_chars"))
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": build_user_prompt(contact, intent)},
    ]
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    def _post(override=None):
        """发一次请求并返回 JSON；404 的 Ollama 诊断逻辑原样保留。

        override：临时覆盖 payload 字段。只用于「预算被 max_tokens 吃光时加
        预算重试」——那种情况重发一份原样的 body 是没有意义的。
        """
        body = payload if not override else {**payload, **override}
        resp = requests.post(url, headers=headers, json=body, timeout=timeout)
        if resp.status_code == 404:
            # Ollama 对两类情况都返回 404：① 请求了没拉取的模型；② 老版本 Ollama 没有 /v1 兼容层
            body = resp.text or ""
            err = body
            try:
                err = resp.json().get("error", body)
            except Exception:
                pass
            # provider 写着 ollama 但 secrets 把 base_url 指向了云端时，也按通用提示走
            if provider != "ollama" or "11434" not in base_url:
                raise RuntimeError(
                    f"{base_url} 返回 404。请检查 ai_secrets.json 的 base_url 是否书写正确"
                    f"（是否需要带 /v1 之类的路径后缀），以及 ai_secrets.json 的 model"
                    f"「{model}」是否为该服务实际支持的模型名。"
                )
            if "does not exist" in err or "not found" in err.lower() or "model" in err.lower():
                raise RuntimeError(
                    f"Ollama 找不到模型「{model}」（返回 404）。\n"
                    f"请确认本地已拉取该模型：\n"
                    f"  ollama list               # 看实际可用的模型名\n"
                    f"  ollama pull {model}        # 若列表里没有就拉一下\n"
                    f"并把 ai_secrets.json 的 model 改成 `ollama list` 里显示的名字"
                    f"（注意 :latest / :7b 等标签要写全）。"
                )
            raise RuntimeError(
                f"Ollama 在 {url} 返回 404，但不是模型问题——很可能是 Ollama 版本过旧、"
                f"未提供 OpenAI 兼容的 /v1 接口。请升级 Ollama（`ollama --version` 看版本），"
                f"或把 ai_secrets.json 的 base_url 改为 Ollama 原生接口 http://localhost:11434/api 另行适配。"
            )
        resp.raise_for_status()
        return resp.json()

    try:
        data = _post()
        content, why = _extract_content(data)
        fr = _finish_reason(data)
        if not content and fr == "length":
            # 预算被吃光（2026-10-21 定位，首条激活连续三次失败的真凶）：
            # 这个模型在「新客户首条激活」这种没有上下文、要自己构思开场白的
            # 场景会进推理模式，而**推理 token 同样计入 max_tokens**。200 的预算
            # 被思维链吃干净 → finish_reason=length、content 一个字都没有。
            # 关键点：这种情况**原样重试必然还是空**（预算没变，同样的推理还会
            # 跑一遍），必须先把预算加上去再试。
            boosted = min(max(max_tokens * 6, 2000), 16000)
            log.warning(
                "AI 正文为空且被 max_tokens 截断（finish_reason=length，"
                "max_tokens=%s，usage=%s），把预算提到 %s 重试",
                max_tokens, data.get("usage"), boosted)
            data = _post({"max_tokens": boosted})
            content, why = _extract_content(data)
            fr = _finish_reason(data)
            if not content and fr == "length":
                raise RuntimeError(
                    "AI 生成失败：模型的推理过程耗尽了 max_tokens（已临时提到 %d），"
                    "正文一个字都没输出。请把 ai_secrets.json 的 max_tokens 调大，"
                    "或换一个非推理模型。" % boosted)
        elif not content:
            # 中转站偶发「200 但没内容」（限流排队/网关丢包/模型空转）。这种情况
            # 重试一次通常就好——直接判失败等于让客户白等一条回复（2026-10-21）。
            log.warning("AI 返回空内容（%s，finish_reason=%s，usage=%s），1.5s 后重试",
                        why, fr, data.get("usage"))
            time.sleep(1.5)
            data = _post()
            content, why = _extract_content(data)
            fr = _finish_reason(data)
        if not content:
            raise RuntimeError(
                "AI 返回空内容（%s；finish_reason=%s；已重试 1 次仍为空）" % (why, fr))
        return _clean(content)
    except requests.exceptions.ConnectionError:
        raise RuntimeError(
            f"连不上大模型服务（{url}）。本地 Ollama 请先 `ollama serve` 并 `ollama pull {model}`；"
            f"云端请检查 base_url / api_key / 网络。"
        )
    except RuntimeError:
        # 上面自己抛的（404 诊断 / 空内容）已经是可读文案，不要再套一层
        raise
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"AI 生成失败：{e}")


def classify(cfg, system, user, timeout=20):
    """轻量二分类/打标：**不套销售系统提示词、不注入知识库**的裸调用。

    给「客户这条消息有没有合作意向」这类判断用。generate() 会把销售人设、
    产品资料全塞进去，让它回答 YES/NO 既浪费 token 又容易被带偏，所以单独走
    一条最短路径：temperature=0、max_tokens 给推理模型留够预算、只要一行结论。

    返回清洗后的短文本；调用方自己解析 YES/NO（用 startswith，别要求全等）。
    """
    base_url, api_key, model, _sp, _temp, max_tokens, _provider = _resolve(cfg)
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    # 推理模型（gpt-5.x 之类）思维链也吃 max_tokens，给太少会返回空正文，
    # 与 generate() 里踩过的坑同源；这里直接给够，不搞二次重试。
    budget = min(max(int(max_tokens or 512) * 3, 1024), 4000)
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0,
        "max_tokens": budget,
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
        resp.raise_for_status()
        content, why = _extract_content(resp.json())
        if not content:
            raise RuntimeError("分类调用返回空内容（%s）" % why)
        return _clean(content)
    except requests.exceptions.ConnectionError:
        raise RuntimeError("连不上大模型服务（%s），无法做意向判定" % url)
    except RuntimeError:
        raise
    except Exception as e:  # noqa: BLE001
        raise RuntimeError("意向判定调用失败：%s" % e)
