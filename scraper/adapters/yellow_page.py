"""公开黄页/行业目录适配器（以“黄页88”照明类目为例的模板）。

注意：黄页站结构常改版、且有反爬与分页机制，本文件是**模板**，
首次使用需按实际 HTML 调整选择器；同样遵守 robots 与限速。
"""
from __future__ import annotations

import logging

from bs4 import BeautifulSoup

from ..base import BaseCrawler
from ..models import Dealer

logger = logging.getLogger("scraper")


class YellowPageCrawler(BaseCrawler):
    source = "yellow_page"

    def collect(self) -> list[Dealer]:
        base_url = self.cfg.get("base_url", "")
        category_path = self.cfg.get("category_path", "")
        max_pages = int(self.cfg.get("max_pages", 5))
        if not base_url:
            logger.warning("yellow_page 未配置 base_url，跳过")
            return []
        results: list[Dealer] = []
        for page in range(1, max_pages + 1):
            if category_path:
                url = f"{base_url.rstrip('/')}/{category_path}?page={page}"
            else:
                url = f"{base_url.rstrip('/')}?page={page}"
            html = self.fetch(url)
            if not html:
                break
            soup = BeautifulSoup(html, "html.parser")
            # TODO: 按黄页88 实际列表项选择器调整（此处为示例 .company-item）
            for item in soup.select(".company-item"):
                name = item.select_one(".name")
                d = Dealer(
                    name=name.get_text(strip=True) if name else "",
                    source=self.source,
                    source_url=url,
                    industry_tag=self.cfg.get("industry_tag", "照明"),
                )
                results.append(d)
        return results
