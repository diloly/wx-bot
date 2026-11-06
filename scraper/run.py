"""采集入口：按 config.json 启用对应数据源，汇总去重后导出 CSV/JSON。

用法（无参数，读默认 config）：
    python -m scraper.run
    python scraper/run.py

带参数运行请走 scraper/cli.py：
    python -m scraper.cli --sources web_search,directory --max 50
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from .adapters.ali1688 import Ali1688Crawler
from .adapters.ic_api import IcApiCrawler
from .adapters.official_site import OfficialSiteCrawler
from .adapters.yellow_page import YellowPageCrawler
from .adapters.directory import DirectoryCrawler
from .search import WebSearchCrawler
from .exporters import export
from .models import Dealer

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "config.json"

CRAWLERS = {
    "web_search": WebSearchCrawler,
    "directory": DirectoryCrawler,
    "official_site": OfficialSiteCrawler,
    "ali1688": Ali1688Crawler,
    "yellow_page": YellowPageCrawler,
    "ic_api": IcApiCrawler,
}


def load_config(path: Path | None = None) -> dict:
    p = path or DEFAULT_CONFIG
    if not p.exists():
        raise FileNotFoundError(f"缺少配置文件: {p}")
    return json.loads(p.read_text(encoding="utf-8"))


def collect_all(cfg: dict, sources_filter: list[str] | None = None,
                max_items: int | None = None) -> list[Dealer]:
    """按 config 启用项采集并汇总；sources_filter 可指定只跑某些源。"""
    sources = cfg.get("sources", {})
    all_dealers: list[Dealer] = []
    for name, scfg in sources.items():
        if sources_filter and name not in sources_filter:
            continue
        if not scfg.get("enabled", False):
            continue
        crawler_cls = CRAWLERS.get(name)
        if not crawler_cls:
            logging.warning("未知数据源: %s", name)
            continue
        logging.info("运行数据源: %s", name)
        try:
            crawler = crawler_cls({**cfg, **scfg})
            dealers = crawler.collect()
            logging.info("  -> %s 采集 %d 条", name, len(dealers))
            all_dealers.extend(dealers)
        except Exception as e:  # noqa: BLE001
            logging.exception("数据源 %s 采集失败（已跳过，继续其他源）: %s", name, e)
        if max_items and len(all_dealers) >= max_items:
            break
    if max_items:
        all_dealers = all_dealers[:max_items]
    return all_dealers


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = load_config()
    dealers = collect_all(cfg)
    out = export(dealers, out_dir=str(HERE / "output"))
    print(f"完成：去重后 {out['count']} 条，CSV={out['csv']}，JSON={out['json']}")


if __name__ == "__main__":
    main()
