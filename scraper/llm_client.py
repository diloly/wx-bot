"""大模型提取层：把抓回的网页正文交给 LLM，抽成结构化经销商字段。

优先复用项目根目录的 ai_secrets.json（OpenAI 兼容接口，模型 your-model）；
若不存在则读 config["llm"]。没有 key 或库未安装时 available()=False，调用方回退正则。
只抽取企业/商家主动公开披露的商业信息，不编造、缺失留空。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

try:
    from openai import OpenAI
except ImportError:  # noqa: F401
    OpenAI = None

from .models import Dealer

logger = logging.getLogger("scraper")

# scraper/llm_client.py -> parents[1] = WechatBot（项目根，含 ai_secrets.json）
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SECRET_PATH = PROJECT_ROOT / "ai_secrets.json"

_SYSTEM = (
    "你是数据抽取助手。从给定网页正文里抽取『企业/经销商』级别的公开联系信息。"
    "只抽取企业或商家主动公开披露的商业信息：公司名、地区、经营范围、官网、"
    "公开座机/业务电话、公开业务联系人姓名。不要编造，没有就留空字符串。"
    "返回 JSON，结构：{\"dealers\": [{\"name\",\"region\",\"business_scope\","
    "\"website\",\"public_phone\",\"contact_name\"}]}，public_phone 多个用' / '分隔。"
)


def _load_secret() -> dict:
    if SECRET_PATH.exists():
        try:
            return json.loads(SECRET_PATH.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            logger.warning("读取 ai_secrets.json 失败: %s", e)
    return {}


class LLMExtractor:
    def __init__(self, cfg: dict):
        self.cfg = cfg or {}
        sec = _load_secret()
        self.base_url = self.cfg.get("base_url") or sec.get("base_url") or ""
        self.api_key = self.cfg.get("api_key") or sec.get("api_key") or ""
        self.model = self.cfg.get("model") or sec.get("model") or "your-model"
        self.client = None
        if OpenAI and self.api_key:
            try:
                self.client = OpenAI(base_url=self.base_url, api_key=self.api_key)
            except Exception as e:  # noqa: BLE001
                logger.warning("LLM 客户端初始化失败: %s", e)

    def available(self) -> bool:
        return self.client is not None

    @staticmethod
    def _clean_json(text: str) -> str:
        # 去掉 ```json 围栏
        text = text.strip()
        if text.startswith("```"):
            text = text.split("```", 2)[1]
            if text.lower().startswith("json"):
                text = text[4:]
        return text.strip()

    def extract(self, page_text: str, url: str = "") -> list[Dealer]:
        if not self.available():
            logger.info("LLM 不可用（缺 key 或库），跳过 LLM 提取")
            return []
        text = page_text[:6000]  # 截断，避免超长/超费
        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": f"来源URL: {url}\n\n正文:\n{text}"},
                ],
                temperature=0,
                max_tokens=1500,
            )
            content = resp.choices[0].message.content or "{}"
            data = json.loads(self._clean_json(content))
            items = data.get("dealers") or data.get("items") or []
            out: list[Dealer] = []
            for it in items:
                out.append(Dealer(
                    name=str(it.get("name", "")).strip(),
                    region=str(it.get("region", "")).strip(),
                    business_scope=str(it.get("business_scope", "")).strip(),
                    website=str(it.get("website", "")).strip() or url,
                    public_phone=str(it.get("public_phone", "")).strip(),
                    contact_name=str(it.get("contact_name", "")).strip(),
                    source="llm",
                    source_url=url,
                    industry_tag=self.cfg.get("industry_tag", "照明"),
                ))
            return out
        except Exception as e:  # noqa: BLE001
            logger.warning("LLM 提取失败: %s", e)
            return []
