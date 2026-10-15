#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI 外发内容生成（OpenAI 兼容接口，支持本地 Ollama / DeepSeek / OpenAI / 通义 等切换）
==================================================================================
- 只负责"生成一条发给客户的微信文案"，不碰发送；发送由 wechat_ui.py 的键盘流程负责。
- 通过 config.ai.provider 切换；本地 Ollama 默认 base_url=http://localhost:11434/v1。
- **保密项（base_url / api_key）放同目录 ai_secrets.json**（不要外发/截图/提交 git），
  优先级高于 config.json 与环境变量；config.json 只放非保密项（provider/model/prompt）。
- 所有主流服务都兼容 OpenAI 的 /v1/chat/completions，所以一套代码走天下。
"""
import json
import logging
import os

import requests

log = logging.getLogger("wechat_pc.ai")

DEFAULT_SYSTEM = (
    "你是一个专业、自然、不油腻的客户关系维护助手。"
    "根据客户信息和本次沟通意图，写一条发给该客户的微信消息。"
    "要求：口语化、像真人、不堆砌营销话术、不超过3句话；"
    "只输出消息正文本身，不要引号、不要解释、不要'好的，这是…'之类的开场白。"
)

# provider -> 默认 base_url / api_key。非保密覆盖项写 config.ai；保密项（base_url/api_key）
# 写 ai_secrets.json，优先级最高。"custom" 用于自建/中转端点：base_url 必须在 secrets 里给。
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
    """读取 ai_secrets.json 里的 base_url / api_key（下划线开头的说明字段忽略）。
    文件不存在 = 返回 {}，由上层按 config.json / 环境变量 / provider 预设继续兜底。
    任何日志/报错都不得打印文件内容——只报"哪个键有问题"。"""
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
        if not isinstance(v, str):
            raise RuntimeError(
                "ai_secrets.json 的 %r 必须是字符串（当前是 %s）" % (k, type(v).__name__))
        out[k] = v
    return out


def _resolve(cfg):
    ai = cfg.get("ai", {})
    provider = (ai.get("provider") or "ollama").lower()
    preset = _PRESETS.get(provider, {})
    secrets = _load_secrets()
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
    # 云端默认给一个通用占位，建议在 config 里写明确模型名。
    default_model = provider if provider == "ollama" else "gpt-4o-mini"
    model = ai.get("model") or default_model
    system_prompt = ai.get("system_prompt") or DEFAULT_SYSTEM
    temperature = ai.get("temperature", 0.7)
    max_tokens = ai.get("max_tokens", 300)
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


def generate(cfg, contact, intent, timeout=60):
    """调用大模型生成一条发给该客户的微信文案，返回清洗后的纯文本。"""
    base_url, api_key, model, system_prompt, temperature, max_tokens, provider = _resolve(cfg)
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
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
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
                    f"（是否需要带 /v1 之类的路径后缀），以及 config.json 的 ai.model"
                    f"「{model}」是否为该服务实际支持的模型名。"
                )
            if "does not exist" in err or "not found" in err.lower() or "model" in err.lower():
                raise RuntimeError(
                    f"Ollama 找不到模型「{model}」（返回 404）。\n"
                    f"请确认本地已拉取该模型：\n"
                    f"  ollama list               # 看实际可用的模型名\n"
                    f"  ollama pull {model}        # 若列表里没有就拉一下\n"
                    f"并把 config.json 的 ai.model 改成 `ollama list` 里显示的名字"
                    f"（注意 :latest / :7b 等标签要写全）。"
                )
            raise RuntimeError(
                f"Ollama 在 {url} 返回 404，但不是模型问题——很可能是 Ollama 版本过旧、"
                f"未提供 OpenAI 兼容的 /v1 接口。请升级 Ollama（`ollama --version` 看版本），"
                f"或把 config.ai.base_url 改为 Ollama 原生接口 http://localhost:11434/api 另行适配。"
            )
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        return _clean(content)
    except requests.exceptions.ConnectionError:
        raise RuntimeError(
            f"连不上大模型服务（{url}）。本地 Ollama 请先 `ollama serve` 并 `ollama pull {model}`；"
            f"云端请检查 base_url / api_key / 网络。"
        )
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"AI 生成失败：{e}")
