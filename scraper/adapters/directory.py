"""照明行业『免 API 自主发现 + 提取』适配器（零第三方 API，主路径）。

完全不需要任何搜索 API key（Bing/SerpAPI 之类）：
  - 直接爬公开黄页 / B2B / 行业目录平台的『列表页』或『站内搜索结果页』来发现企业；
  - 这些平台本身就是公开的经销商聚合地，遍历它们即可，无需调用任何搜索 API；
  - 抓到详情页后用基类 fetch_and_extract 抽字段（优先 LLM，回退正则）。

发现的两类种子（全部在 config 里，代码零硬编码错误）：
  1) list_pages: 直接给的列表页 / 参展商名录页 URL；
  2) site_search_templates × keywords: 对公开平台提交关键词『站内搜索』（普通 GET，免 API），
     例如 "https://www.huangye88.com/search?keyword={kw}" 中的 {kw} 会被 URL 编码替换。

合规：只采企业主动公开信息；respect_robots / 限速 / 退避继承自 BaseCrawler。
注：各平台搜索/列表页 URL 结构可能改版，首次在新站点实跑后，把真实 URL 给我即可校准选择器。
"""
from __future__ import annotations

import logging
from urllib.parse import quote, urljoin, urlparse

from bs4 import BeautifulSoup

from ..base import BaseCrawler

logger = logging.getLogger("scraper")

# 行业关键词（用于站内搜索发现，覆盖照明/灯具/灯饰/工程/古镇等口径）
DEFAULT_KEYWORDS = [
    "照明经销商", "LED照明代理", "灯饰批发", "商业照明", "工程照明", "古镇灯饰厂家",
]

# 链接文本过滤用的短 token（命中其一即视为企业条目候选）
MATCH_TOKENS = ["照明", "灯具", "灯饰", "灯", "LED", "亮化"]
# 翻页链接文本启发式
NEXT_TOKENS = ["下一页", "下页", "next", "»", "›", "更多"]
# 链接文本黑名单（导航/功能类，不算企业条目）
SKIP_TEXT = {
    "首页", "上一页", "下一页", "下页", "末页", "尾页", "联系我们", "关于我们",
    "网站地图", "注册", "登录", "免费发布", "发布供求", "客服", "帮助", "反馈",
}
# 内置『站内搜索』模板（示例，需按真实平台校准；用户可在 config 覆盖）。
# 这些是公开黄页/B2B 平台的站内搜索入口，普通 GET 即可，无需 API key。
DEFAULT_SEARCH_TEMPLATES = [
    "https://www.huangye88.com/search?keyword={kw}",
    "https://www.11467.com/search/?kw={kw}",
    "https://www.hc360.com/search?keyword={kw}",
]


class DirectoryCrawler(BaseCrawler):
    source = "directory"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.max_pages = int(self.cfg.get("max_pages", 3))
        self.list_pages = self.cfg.get("list_pages") or []
        self.keywords = self.cfg.get("keywords") or DEFAULT_KEYWORDS
        self.use_site_search = bool(self.cfg.get("use_site_search", True))
        self.search_templates = (
            self.cfg.get("site_search_templates") or DEFAULT_SEARCH_TEMPLATES
        )

    def collect(self) -> list:
        detail_urls = self._discover()
        results: list = []
        for url in detail_urls:
            results.extend(self.fetch_and_extract(url))
        return results

    # —— 自主发现：列表页 + 站内搜索结果页，动态翻页 + 短 token 过滤 ——
    def _discover(self) -> list[str]:
        seeds = list(self.list_pages)
        if self.use_site_search:
            for tpl in self.search_templates:
                for kw in self.keywords:
                    seeds.append(tpl.replace("{kw}", quote(kw)))
        # 去重种子（保持顺序）
        seen_seed, uniq = set(), []
        for s in seeds:
            if s not in seen_seed:
                seen_seed.add(s)
                uniq.append(s)

        found: list[str] = []
        for base in uniq:
            found.extend(self._crawl_pages(base))
            if len(found) >= 300:  # 安全上限，避免意外爆炸
                break

        # 去重详情 URL（按规范化）
        seen, out = set(), []
        for u in found:
            norm = self._normalize(u)
            if norm not in seen:
                seen.add(norm)
                out.append(u)
        logger.info("directory 共发现 %d 个候选详情页（去重后）", len(out))
        return out

    def _crawl_pages(self, base: str) -> list[str]:
        found: list[str] = []
        seen_pages: set[str] = set()
        queue = [base]
        pages_crawled = 0
        while queue and pages_crawled < self.max_pages:
            page_url = queue.pop(0)
            if page_url in seen_pages:
                continue
            seen_pages.add(page_url)
            pages_crawled += 1
            html = self.fetch(page_url)
            if not html:
                break
            soup = BeautifulSoup(html, "html.parser")
            # 1) 企业条目候选：链接文本命中短 token、长度适中、是绝对 http(s) 链接
            for a in soup.find_all("a", href=True):
                txt = a.get_text(strip=True)
                href = a["href"].strip()
                if not href.startswith("http"):
                    continue
                if (txt and txt not in SKIP_TEXT and 2 < len(txt) < 40
                        and any(tok in txt for tok in MATCH_TOKENS)):
                    found.append(href)
            # 2) 翻页：找下一页类链接并继续抓，最多 max_pages 层
            nxt = self._find_next_page(soup, page_url)
            if nxt and nxt not in seen_pages:
                queue.append(nxt)
        return found

    @staticmethod
    def _find_next_page(soup: BeautifulSoup, page_url: str) -> str | None:
        best: str | None = None
        for a in soup.find_all("a", href=True):
            txt = a.get_text(strip=True).lower()
            href = a["href"].strip()
            if not href or href.startswith("#") or href.startswith("javascript"):
                continue
            if any(tok in txt for tok in NEXT_TOKENS) or txt.isdigit():
                if txt.isdigit() and int(txt) <= 1:  # 排除首页“1”
                    continue
                abs_href = urljoin(page_url, href)
                if urlparse(abs_href).netloc:  # 合法 URL
                    best = abs_href
        return best

    @staticmethod
    def _normalize(url: str) -> str:
        p = urlparse(url)
        path = p.path.rstrip("/")
        return f"{p.netloc}{path}".lower()
