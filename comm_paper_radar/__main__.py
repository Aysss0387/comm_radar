import argparse
from pathlib import Path

from .config import load_settings
from .pipeline import run_search, run_weekly


def main() -> None:
    parser = argparse.ArgumentParser(description="传播学/计算传播论文雷达")
    parser.add_argument("--config", default="config/settings.yml", help="配置文件路径")
    parser.add_argument("--base-dir", default=".", help="项目根目录")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="生成自动周报")
    run_parser.add_argument("--from", dest="from_date", default=None, help="开始日期 YYYY-MM-DD")
    run_parser.add_argument("--to", dest="to_date", default=None, help="结束日期 YYYY-MM-DD")
    run_parser.add_argument("--dry-run", action="store_true", help="只运行流程，不写文件")

    search_parser = subparsers.add_parser("search", help="手动检索并生成专题报告")
    search_parser.add_argument("--profile", choices=["communication_core", "anti_fraud", "ai_communication"], default=None)
    search_parser.add_argument("--keywords", default="", help="关键词，多个词可用逗号分隔")
    search_parser.add_argument("--journal", default="", help="关键期刊/会议名称")
    search_parser.add_argument("--region", choices=["all", "domestic", "international"], default="all")
    search_parser.add_argument("--from", dest="from_date", default=None, help="开始日期 YYYY-MM-DD")
    search_parser.add_argument("--to", dest="to_date", default=None, help="结束日期 YYYY-MM-DD")
    search_parser.add_argument("--top", "--limit", dest="top", type=int, default=20, help="报告篇数")
    search_parser.add_argument("--report", default=None, help="报告输出路径")
    search_parser.add_argument("--include-seen", action="store_true", help="包含历史已推荐论文")
    search_parser.add_argument("--mark-recommended", action="store_true", help="将手动报告论文写入推荐历史")
    search_parser.add_argument("--dry-run", action="store_true", help="只运行流程，不写文件")

    args = parser.parse_args()
    base_dir = Path(args.base_dir)
    settings = load_settings(args.config)

    if args.command == "run":
        selected = run_weekly(
            settings,
            base_dir,
            from_date=args.from_date,
            to_date=args.to_date,
            dry_run=args.dry_run,
        )
    else:
        selected = run_search(
            settings,
            base_dir,
            profile=args.profile,
            keywords=args.keywords,
            journal=args.journal,
            region=args.region,
            from_date=args.from_date,
            to_date=args.to_date,
            top=args.top,
            report_path=Path(args.report) if args.report else None,
            include_seen=args.include_seen,
            mark_recommended=args.mark_recommended,
            dry_run=args.dry_run,
        )
    print(f"Selected {len(selected)} papers")
    for index, paper in enumerate(selected, start=1):
        print(f"{index}. [{paper.total_score:.2f}] {paper.title}")


if __name__ == "__main__":
    main()

