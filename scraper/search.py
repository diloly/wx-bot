"""Web 搜索发现适配器：通过合规的搜索 API 自主发现照明经销商页面。

这是『程序自己找、不用你列 URL』的合规正解：
  - 调用 Bing Web Search / SerpAPI 等**正规搜索 API** 拿到结果 URL；
  - 不抓取搜索引擎 HTML 结果页（违反其服务条款，且易封）；
  - 拿到 URL 后复用基类的 fetch_and_extract（优先 LLM、回退正则）提取字段。

使用前需自行申请一个搜索 API key 并填入 config（这是凭证，不是『内容』）。
未配置 key 时本适配器自动跳过，由 directory 等无 key 源兜底。
"""
from __future__ import annotations

import logging
from urllib.parse import urlparse

from .base import BaseCrawler

logger = logging.getLogger("scraper")

# 默认搜索词（照明行业），可在 config 的 web_search.queries 覆盖
DEFAULT_QUERIES = [
    "照明 经销商 联系方式",
    "LED照明 代理商 联系方式",
    "灯饰 批发 厂家 联系",
    "商业照明 工程照明 代理",
    "古镇 灯饰 厂家 官网",
]

# 黑名单：明显是社媒/新闻/问答/论坛的域名，直接丢弃，避免抓到无关页。
# 注意：不采用白名单——经销商自有官网（如 snlighting.com.cn）不在任何白名单里，
# 用白名单会把真目标误杀。我们只挡明显噪声，其余结果都视为候选去抓取提取。
BLOCKED_DOMAINS = (
    "weibo.com", "zhihu.com", "douyin.com", "xiaohongshu.com", "toutiao.com",
    "163.com", "qq.com", "sina.com.cn", "sohu.com", "ifeng.com", "baidu.com",
    "csdn.net", "cnblogs.com", "jianshu.com", "bilibili.com", "youtube.com",
    "facebook.com", "twitter.com", "wikipedia.org",
)


class WebSearchCrawler(BaseCrawler):
    source = "web_search"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.provider = self.cfg.get("provider", "bing")
        self.api_key = self.cfg.get("api_key", "")
        self.endpoint = self.cfg.get(
            "endpoint", "https://api.bing.microsoft.com/v7.0/search"
        )
        self.queries = self.cfg.get("queries") or DEFAULT_QUERIES
        self.max_per_query = int(self.cfg.get("max_results_per_query", 20))
        self.market = self.cfg.get("market", "zh-CN")
        self.blocked = self.cfg.get("blocked_domains") or list(BLOCKED_DOMAINS)

    def collect(self) -> list[Dealer]:
        if not self.api_key:
            logger.warning("web_search 未配置 api_key，跳过（需自行申请搜索 API key）")
            return []
        urls = self._discover()
        results: list[Dealer] = []
        for url in urls:
            results.extend(self.fetch_and_extract(url))
        return results

    # —— 调搜索 API 拿到候选 URL ——
    def _discover(self) -> list[str]:
        found: list[str] = []
        for q in self.queries:
            try:
                items = self._search_once(q)
            except Exception as e:  # noqa: BLE001
                logger.warning("搜索失败 [%s]: %s", q, e)
                continue
            for url in items:
                if self._accept(url):
                    found.append(url)
            logger.info("搜索词[%s] -> %d 条结果", q, len(items))
        # 去重
        seen, out = set(), []
        for u in found:
            if u not in seen:
                seen.add(u)
                out.append(u)
        logger.info("web_search 共发现 %d 个候选页（去重后）", len(out))
        return out

    def _search_once(self, query: str) -> list[str]:
        if self.provider == "serpapi":
            params = {
                "engine": "google", "q": query, "api_key": self.api_key,
                "num": self.max_per_query, "gl": "cn", "hl": "zh-cn",
            }
            resp = self.session.get("https://serpapi.com/search.json", params=params, timeout=20)
            data = resp.json()
            return [r.get("link") for r in data.get("organic_results", []) if r.get("link")]
        # 默认 Bing Web Search v7
        headers = {"Ocp-Apim-Subscription-Key": self.api_key}
        params = {"q": query, "count": self.max_per_query, "mkt": self.market}
        resp = self.session.get(self.endpoint, headers=headers, params=params, timeout=20)
        data = resp.json()
        pages = (data.get("webPages") or {}).get("value") or []
        return [p.get("url") for p in pages if p.get("url")]

    def _accept(self, url: str) -> bool:
        if not url or not url.startswith("http"):
            return False
        host = urlparse(url).netloc.lower()
        if any(d in host for d in self.blocked):
            return False  # 挡掉社媒/新闻/问答噪声
        return True
