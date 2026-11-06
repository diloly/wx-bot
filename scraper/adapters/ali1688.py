"""1688 / 阿里巴巴商家页适配器（模板）。

说明（重要）：
  - 1688 商家页公开可见字段通常只有企业名、主营、地区、部分公开联系方式；
  - 完整的“联系方式/手机号”往往需登录千牛或在“公司档案”页，属登录后内容，
    本适配器**不携带登录态**、不绕过任何防护，只抓公开可见部分；
  - 1688 反爬较强，生产使用建议在 cfg 里填入你自己的浏览器 Cookie（自愿提供），
    并保持限速；请勿高频请求。
"""
from __future__ import annotations

import logging
import re

from bs4 import BeautifulSoup

from ..base import BaseCrawler
from ..models import Dealer

logger = logging.getLogger("scraper")


class Ali1688Crawler(BaseCrawler):
    source = "ali1688"

    def collect(self) -> list[Dealer]:
        shop_urls = self.cfg.get("shop_urls") or []
        keywords = self.cfg.get("keywords") or []
        results: list[Dealer] = []
        for url in shop_urls:
            html = self.fetch(url)
            if not html:
                continue
            soup = BeautifulSoup(html, "html.parser")
            title = soup.title.string if soup.title and soup.title.string else ""
            d = Dealer(
                name=title.strip() if title else url,
                source=self.source,
                source_url=url,
                industry_tag=self.cfg.get("industry_tag", "照明"),
            )
            # 1688 页面结构多变，这里仅做兜底：抓页面里出现的手机号（若有公开）
            text = soup.get_text(" ", strip=True)
            m = re.search(r"(1[3-9]\d{9})", text)
            if m:
                d.public_phone = m.group(1)
            results.append(d)
        if keywords:
            logger.info(
                "关键词搜索模式建议在 run 时构造搜索 URL；当前模板未内置反爬搜索，"
                "行业批量检索更推荐改用 ic_api（工商 API）。"
            )
        return results
