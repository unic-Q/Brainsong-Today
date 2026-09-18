import argparse
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .pipeline import run


def main():
    parser = argparse.ArgumentParser(description="Brainsong Today 官方智谱每日简报")
    parser.add_argument("--root", default=".")
    parser.add_argument("--offline", action="store_true", help="只使用本地样例；不联网、不调用模型、不推送")
    parser.add_argument("--fixture", default="tests/fixtures/briefing.json")
    parser.add_argument("--no-ai", action="store_true", help="关闭模型筛选摘要；不关闭付费搜索")
    parser.add_argument("--send", action="store_true", help="明确启用飞书发送，默认仅生成本地报告")
    parser.add_argument("--state")
    parser.add_argument("--resume", action="store_true", help="复用候选缓存，执行配置中的定向补搜，不重抓信源列表")
    parser.add_argument("--cached-only", action="store_true", help="只补分析缓存候选，不执行信源列表采集或常规搜索")
    args = parser.parse_args()
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    try:
        result = run(args.root, today, offline=args.offline, no_ai=args.no_ai, send=args.send,
                     fixture=Path(args.root) / args.fixture, state_path=args.state, resume=args.resume, cached_only=args.cached_only)
        print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        logs = Path(args.root) / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        with (logs / f"{today}.log").open("a", encoding="utf-8") as handle:
            handle.write(f"\n{today} | fatal | {type(exc).__name__}\n")
        print("运行未完成，错误类型：" + type(exc).__name__ + "。请查看logs和state，不输出密钥或服务器响应。")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
