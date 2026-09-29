import argparse
import json
import signal
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .pipeline import load, run
from .state import State


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
    parser.add_argument("--prune-only", action="store_true", help="只清理过期及无日期候选，不搜索、不推送")
    args = parser.parse_args()
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    # Actions cancellation sends SIGINT then SIGTERM. Unwind the pipeline's
    # finally block so completed diagnostics and candidates can be uploaded.
    def cancelled(signum, frame):
        raise SystemExit(130)
    previous = {sig: signal.signal(sig, cancelled) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        if args.prune_only:
            if args.send or args.offline or args.resume or args.cached_only:
                parser.error("--prune-only 不能与采集、发送或离线选项组合")
            cfg, _, _, _ = load(args.root)
            path = args.state or Path(args.root) / 'state/brainsong.sqlite3'
            state = State(path)
            try:
                before = state.db.execute('SELECT count(*) FROM candidate_inputs').fetchone()[0]
                state.prune(today, cfg['lookback_days'], drop_undated=True,
                            use_first_reported=cfg.get('strict_event_freshness', False))
                after = state.db.execute('SELECT count(*) FROM candidate_inputs').fetchone()[0]
            finally:
                state.close()
            print(json.dumps({'prune_only': True, 'removed': before - after,
                              'remaining': after}, ensure_ascii=False))
            return
        result = run(args.root, today, offline=args.offline, no_ai=args.no_ai, send=args.send,
                     fixture=Path(args.root) / args.fixture, state_path=args.state, resume=args.resume, cached_only=args.cached_only)
        print(json.dumps(result, ensure_ascii=False))
        if args.send and not result['sent']:
            print('未完成发送，请查看候选数量和发送记录；本次任务不标记成功。', flush=True)
            raise SystemExit(2)
    except Exception as exc:
        logs = Path(args.root) / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        with (logs / f"{today}.log").open("a", encoding="utf-8") as handle:
            handle.write(f"\n{today} | fatal | {type(exc).__name__}\n")
        print("运行未完成，错误类型：" + type(exc).__name__ + "。请查看logs和state，不输出密钥或服务器响应。")
        raise SystemExit(1) from None
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    main()
