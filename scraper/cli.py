"""命令行入口：灵活选择数据源、输出目录、采集上限。

示例：
    # 默认读 scraper/config.json，跑所有 enabled 的源
    python -m scraper.cli

    # 只跑搜索 + 黄页，最多 50 条，输出到 ./out
    python -m scraper.cli --sources web_search,directory --max 50 --out ./out

    # 调试：只看 INFO 以上日志
    python -m scraper.cli --log DEBUG
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

from .run import load_config, collect_all
from .exporters import export

HERE = Path(__file__).resolve().parent


def main(argv: list[str] | None = None) -> dict:
    parser = argparse.ArgumentParser(
        prog="scraper", description="照明经销商公开信息采集（合规、限速、尊重 robots）"
    )
    parser.add_argument(
        "--config", default=str(HERE / "config.json"),
        help="配置文件路径（默认 scraper/config.json）",
    )
    parser.add_argument(
        "--sources", default=None,
        help="逗号分隔的数据源名，覆盖 config 的 enabled 项；如 web_search,directory",
    )
    parser.add_argument(
        "--out", default=None,
        help="输出目录（默认 scraper/output）",
    )
    parser.add_argument(
        "--max", type=int, default=None,
        help="全局采集条数上限（去重前）",
    )
    parser.add_argument(
        "--log", default="INFO",
        help="日志级别：DEBUG / INFO / WARNING",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cfg = load_config(Path(args.config))
    sources_filter = (
        [s.strip() for s in args.sources.split(",") if s.strip()]
        if args.sources else None
    )
    dealers = collect_all(cfg, sources_filter=sources_filter, max_items=args.max)
    out_dir = args.out or str(HERE / "output")
    out = export(dealers, out_dir=out_dir)
    print(f"完成：去重后 {out['count']} 条，CSV={out['csv']}，JSON={out['json']}")
    return out


if __name__ == "__main__":
    main()
