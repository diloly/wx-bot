"""经销商企业信息数据模型与去重。

只承载企业**主动公开**的商业信息字段；不采集任何私人/非公开 PII。
public_phone 字段指企业自愿披露的商务/总机电话（可能是手机或座机），
不是私人手机号。
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from datetime import datetime, timezone


@dataclass
class Dealer:
    # —— 企业基础信息（公开）——
    name: str = ""                  # 企业名
    region: str = ""                # 地区（省/市/区）
    business_scope: str = ""        # 经营范围 / 主营
    website: str = ""               # 官网或店铺 URL
    public_phone: str = ""          # 企业主动公开的业务/总机电话（手机或座机）
    # —— 公开联系人姓名（工商公开或官网披露的业务联系人）——
    contact_name: str = ""          # 法人/负责人/业务联系人姓名
    # —— 来源与标签 ——
    source: str = ""                # 数据源标识
    source_url: str = ""            # 抓取来源 URL
    industry_tag: str = "照明"       # 行业标签
    fetched_at: str = ""            # 采集时间（ISO8601）

    def dedup_key(self) -> str:
        # 去重主键：企业名 + (电话或官网)，任一非空即可拼出稳定指纹
        parts = [self.name.strip().lower()]
        if self.public_phone.strip():
            parts.append(self.public_phone.strip())
        if self.website.strip():
            parts.append(self.website.strip().lower())
        raw = "|".join(p for p in parts if p)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def is_usable(self) -> bool:
        # 至少要有企业名或电话之一，才认为是一条有效记录
        return bool(self.name.strip() or self.public_phone.strip())

    def to_dict(self) -> dict:
        d = asdict(self)
        d["fetched_at"] = self.fetched_at or datetime.now(timezone.utc).isoformat()
        return d
