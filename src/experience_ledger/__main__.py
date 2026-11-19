"""Local single-user entry point; no credentials or network model required."""

import argparse
import json
from pathlib import Path

from .service import Ledger, LedgerError


def main():
    parser = argparse.ArgumentParser(description="传火 Firelinker · 本地经验工作台")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("serve", "demo", "export", "evaluate"):
        action = sub.add_parser(command)
        action.add_argument("--data-dir", type=Path, default=Path(".local-data"))
        action.add_argument("--domains-dir", type=Path)
        if command == "serve":
            action.add_argument("--port", type=int, default=5057)
        elif command == "export":
            action.add_argument("--case-id", required=True)
            action.add_argument("--output", type=Path, required=True)
        elif command == "evaluate":
            action.add_argument("--output", type=Path, required=True)
            action.add_argument("--run-model", action="store_true", help="显式调用已配置模型；默认仅检查合成情境")
    args = parser.parse_args()
    try:
        if args.command == "evaluate":
            from .evaluation import run_evaluation
            if args.run_model:
                from .model_settings import ModelSettings
                with ModelSettings(args.data_dir).use() as planner:
                    report = run_evaluation(planner, run_model=True)
            else:
                report = run_evaluation()
            with args.output.open("x", encoding="utf-8") as target:
                json.dump(report, target, ensure_ascii=False, indent=2)
            print(f"评测记录已保存到 {args.output.resolve()}；语义质量需按报告逐项人工评阅。")
        elif args.command == "serve":
            from .web import create_app
            create_app(args.data_dir, args.domains_dir).run(host="127.0.0.1", port=args.port, debug=False)
        else:
            ledger = Ledger(args.data_dir, args.domains_dir)
            if args.command == "demo":
                from .demo import seed_demo
                cases = seed_demo(ledger)
                print(json.dumps([{"id": c["id"], "title": c["title"],
                                   "sources": len(c["sources"]), "turns": len(c["turns"]),
                                   "candidates": [{"id": item["id"], "status": item["status"]}
                                                  for item in c["candidates"]]}
                                  for c in cases], ensure_ascii=False, indent=2))
            else:
                from .exports import export_case
                excerpt = export_case(ledger, args.case_id)
                with args.output.open("x", encoding="utf-8") as target:
                    json.dump(excerpt, target, ensure_ascii=False, indent=2)
                print(f"已导出到 {args.output.resolve()}")
    except (LedgerError, OSError) as exc:
        parser.exit(1, f"错误：{exc}\n")


if __name__ == "__main__":
    main()
