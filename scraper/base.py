"""合规爬虫基类：限速 + robots 尊重 + 重试退避 + 共享的「抓取→提取」能力。

设计原则（写进代码，非口头约束）：
  - 默认尊重 robots.txt，对不允许的路径直接跳过；
  - 请求间强制最小间隔（限速），避免对目标站造成压力；
  - 失败指数退避重试，不暴力穷举；
  - 仅抓取公开页面，不携带登录态去碰付费/登录墙后的内容；
  - 只采企业主动公开披露的商业信息，不碰私人非公开 PII。
"""
from __future__ import annotations

import logging
import re
import time
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests

from .models import Dealer
from .llm_client import LLMExtractor

logger = logging.getLogger("scraper")

PROVINCES = [
    "北京", "天津", "上海", "重庆", "河北", "山西", "辽宁", "吉林", "黑龙江",
    "江苏", "浙江", "安徽", "福建", "江西", "山东", "河南", "湖北", "湖南",
    "广东", "海南", "四川", "贵州", "云南", "陕西", "甘肃", "青海", "台湾",
    "内蒙古", "广西", "西藏", "宁夏", "新疆", "香港", "澳门",
]
RE_MOBILE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")
RE_TEL = re.compile(r"(?<!\d)(0\d{2,3}[-\s]?\d{7,8})(?!\d)")


class BaseCrawler:
    source = "base"

    def __init__(self, cfg: dict):
        self.cfg = cfg or {}
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": self.cfg.get(
                "user_agent",
                "Mozilla/5.0 (compatible; DealerScraper/1.0; +public-collection)",
            ),
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        self.min_interval = float(self.cfg.get("min_interval_seconds", 3.0))
        self.max_retries = int(self.cfg.get("max_retries", 3))
        self.backoff_base = float(self.cfg.get("backoff_base", 2.0))
        self.respect_robots = bool(self.cfg.get("respect_robots", True))
        # 合规严格开关：true 时正则兜底只保留座机，丢弃私人手机
        self.exclude_mobile = bool(
            (self.cfg.get("compliance") or {}).get("exclude_personal_mobile", False)
        )
        self._last_req = 0.0
        self._robots_cache: dict[str, RobotFileParser] = {}
        # 所有适配器共享同一个大模型提取器
        self.llm = LLMExtractor({**self.cfg, "industry_tag": self.cfg.get("industry_tag", "照明")})

    # —— 限速 ——
    def _throttle(self):
        gap = self.min_interval - (time.time() - self._last_req)
        if gap > 0:
            time.sleep(gap)
        self._last_req = time.time()

    # —— robots ——
    def _robots_allows(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        host = urlparse(url).netloc
        rp = self._robots_cache.get(host)
        if rp is None:
            rp = RobotFileParser()
            robots_url = f"https://{host}/robots.txt"
            try:
                rp.set_url(robots_url)
                rp.read()
            except Exception as e:  # noqa: BLE001
                logger.warning("robots 读取失败 %s: %s（按允许处理）", robots_url, e)
            self._robots_cache[host] = rp
        return rp.can_fetch(self.session.headers["User-Agent"], url)

    # —— 带限速/重试/robots 的 fetch ——
    def fetch(self, url: str, timeout: int = 20) -> str | None:
        if not self._robots_allows(url):
            logger.info("robots 禁止抓取，跳过: %s", url)
            return None
        for attempt in range(1, self.max_retries + 1):
            self._throttle()
            try:
                resp = self.session.get(url, timeout=timeout)
                if resp.status_code == 200:
                    resp.encoding = resp.apparent_encoding or "utf-8"
                    return resp.text
                if resp.status_code in (429, 503):
                    wait = self.backoff_base ** attempt
                    logger.warning("限流 %s，退避 %.1fs", resp.status_code, wait)
                    time.sleep(wait)
                    continue
                logger.warning("HTTP %s @ %s", resp.status_code, url)
                return None
            except Exception as e:  # noqa: BLE001
                wait = self.backoff_base ** attempt
                logger.warning("请求异常 %s（第%d次，%.1fs后重试）: %s", url, attempt, wait, e)
                time.sleep(wait)
        return None

    # —— 共享：抓取一个 URL 并提取经销商（优先 LLM，回退正则）——
    def fetch_and_extract(self, url: str) -> list[Dealer]:
        html = self.fetch(url)
        if not html:
            return []
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")
            text = soup.get_text(" ", strip=True)
        except Exception as e:  # noqa: BLE001
            logger.warning("解析失败 %s: %s", url, e)
            return []
        dealers: list[Dealer] = []
        if self.llm.available():
            dealers = self.llm.extract(text, url)
        if not dealers:
            dealers = [self._regex_extract(soup, text, url)]
        for d in dealers:
            d.source = self.source  # 统一标记为当前数据源
        return dealers

    # —— 正则兜底提取（公共）——
    def _regex_extract(self, soup, text, url) -> Dealer:
        d = Dealer(
            source=self.source, source_url=url,
            industry_tag=self.cfg.get("industry_tag", "照明"), website=url,
        )
        if soup.title and soup.title.string:
            d.name = soup.title.string.strip()
        phones: list[str] = []
        for m in RE_MOBILE.findall(text) + RE_TEL.findall(text):
            m = m.strip()
            if not m or m in phones:
                continue
            if self.exclude_mobile and RE_MOBILE.fullmatch(m):
                continue  # 严格模式丢弃私人手机，仅留座机
            phones.append(m)
        d.public_phone = " / ".join(phones[:3])
        for prov in PROVINCES:
            if prov in text:
                d.region = prov
                break
        return d

    # 子类实现：输出 Dealer 列表
    def collect(self) -> list[Dealer]:
        raise NotImplementedError
