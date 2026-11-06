"""工商数据 API 适配器（企查查 / 天眼查 开放 API 模板）。

需要你自行申请 API key，并遵守其调用配额与《个人信息保护法》——
只取**企业工商公开字段**（企业名、法人、注册地、经营范围、公开电话），
不取股东/高管的私人手机号等未公开 PII。
"""
from __future__ import annotations

import logging

from ..base import BaseCrawler
from ..models import Dealer

logger = logging.getLogger("scraper")


class IcApiCrawler(BaseCrawler):
    source = "ic_api"

    # 不同服务商 endpoint 不同；以企查查“企业关键词搜索”为例，需按其文档补全签名
    def collect(self) -> list[Dealer]:
        provider = self.cfg.get("provider", "qcc")  # qcc / tianyancha
        api_key = self.cfg.get("api_key", "")
        keyword = self.cfg.get("keyword", "照明 经销商")
        if not api_key:
            logger.warning("ic_api 未配置 api_key，跳过（需自行申请）")
            return []
        # TODO: 按所选服务商的开放 API 文档实现签名与请求，例如：
        #   resp = self.session.get(endpoint, params={"keyword": keyword, "key": api_key})
        #   for item in resp.json().get("result", []):
        #       Dealer(name=item["Name"], region=item["Region"], ...)
        logger.info("ic_api(%s) 适配器已就绪，请填入 endpoint 与签名逻辑后启用。", provider)
        return []
