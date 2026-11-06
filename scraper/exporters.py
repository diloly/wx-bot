"""去重 + 导出 CSV / JSON。

CSV 用 utf-8-sig 防止 Excel 打开乱码。
"""
from __future__ import annotations

import csv
import json
import logging
from pathlib import Path

from .models import Dealer

logger = logging.getLogger("scraper")

FIELDS = [
    "name", "region", "business_scope", "website", "public_phone",
    "contact_name", "source", "source_url", "industry_tag", "fetched_at",
]


def dedupe(dealers: list[Dealer]) -> list[Dealer]:
    seen: dict[str, Dealer] = {}
    for d in dealers:
        if not d.is_usable():
            continue
        k = d.dedup_key()
        if k not in seen:
            seen[k] = d
    return list(seen.values())


def export(dealers: list[Dealer], out_dir: str = "scraper/output") -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    clean = dedupe(dealers)
    csv_path = out / "dealers.csv"
    json_path = out / "dealers.json"
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for d in clean:
            row = d.to_dict()
            w.writerow({k: row.get(k, "") for k in FIELDS})
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump([d.to_dict() for d in clean], f, ensure_ascii=False, indent=2)
    logger.info("导出 %d 条（原始 %d），CSV=%s JSON=%s",
                len(clean), len(dealers), csv_path, json_path)
    return {"count": len(clean), "csv": str(csv_path), "json": str(json_path)}
