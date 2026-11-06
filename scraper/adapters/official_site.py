"""官网“联系我们”页通用解析器（最实用，可立即使用）。

适用：你在 config 的 official_site.urls 里提供一批目标企业官网/联系页 URL，
本适配器抓取并提取公开信息。
提取以正则为主（网页结构各异，无法保证 100% 命中），命中公开的业务电话、
邮箱、地址、公司名。绝不登录、不碰付费墙。
"""
from __future__ import annotations

import logging
import re

from bs4 import BeautifulSoup

from ..base import BaseCrawler
from ..models import Dealer

logger = logging.getLogger("scraper")

# 手机号（中国大陆）
RE_MOBILE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")
# 座机：区号-号码
RE_TEL = re.compile(r"(?<!\d)(0\d{2,3}[-\s]?\d{7,8})(?!\d)")
RE_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
RE_REGION = re.compile(
    r"(广东|浙江|江苏|山东|福建|北京|上海|天津|重庆|河北|河南|湖北|湖南|四川|安徽|"
    r"江西|广西|云南|贵州|陕西|山西|辽宁|吉林|黑龙江|内蒙古|甘肃|青海|宁夏|新疆|"
    r"海南|西藏|香港|澳门|台湾)\s*([一-龥]{1,6}?(市|区|县|镇|州))?"
)


class OfficialSiteCrawler(BaseCrawler):
    source = "official_site"

    def collect(self) -> list[Dealer]:
        urls = self.cfg.get("urls") or []
        results: list[Dealer] = []
        for url in urls:
            html = self.fetch(url)
            if not html:
                continue
            soup = BeautifulSoup(html, "html.parser")
            text = soup.get_text(" ", strip=True)
            dealer = Dealer(
                website=url,
                source=self.source,
                source_url=url,
                industry_tag=self.cfg.get("industry_tag", "照明"),
            )
            # 公司名：优先 title，其次 h1
            if soup.title and soup.title.string:
                dealer.name = soup.title.string.strip()
            h1 = soup.find("h1")
            if h1 and not dealer.name:
                dealer.name = h1.get_text(strip=True)
            # 电话：优先 tel: 链接，其次正则
            tel_links = [a.get("href", "")[4:] for a in soup.find_all("a", href=re.compile(r"^tel:"))]
            phones: list[str] = []
            for m in RE_MOBILE.findall(text) + tel_links + RE_TEL.findall(text):
                m = m.strip()
                if m and m not in phones:
                    phones.append(m)
            dealer.public_phone = " / ".join(phones[:3])
            # 邮箱（附在经营范围里，便于单文件查看）
            emails = RE_EMAIL.findall(text)
            if emails:
                dealer.business_scope = (dealer.business_scope + f" 邮箱:{emails[0]}").strip()
            # 地区
            reg = RE_REGION.search(text)
            if reg:
                dealer.region = reg.group(0)
            results.append(dealer)
        return results
