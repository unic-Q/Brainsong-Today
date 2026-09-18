"""No AI, no search-provider fees, no DB writes or messaging. Text metadata only."""
from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from brainsong.collect import Reader, collect_source, metadata
from repo_courier.matching import match_rules

ROOT = Path(__file__).resolve().parents[1]


def probe(spec, today, rules):
    row = {k: spec.get(k, "") for k in ("id", "name", "url", "kind", "group")}
    if spec["kind"] in {"search", "credential"}:
        return dict(row, status="未调用付费搜索" if spec["kind"] == "search" else "需要独立凭证")
    reader = Reader()
    try:
        items = collect_source(spec, reader, today, rules, filter_relevance=False)
        hits = [a for a in items if match_rules(a.title, a.summary, a.body, rules)]
        dated = [a for a in items if a.published]
        row.update(status="可解析" if items else "时间窗内零条", parsed=len(items), dated=len(dated), relevant=len(hits))
        # One real detail page per source; no general homepage availability shortcuts.
        sample = next(iter(hits or items), None)
        if sample:
            row["sample"] = {"title": sample.title, "url": sample.url, "date": sample.published,
                             "summary_chars": len(sample.summary)}
            try:
                metadata(sample, reader.get(sample.url))
                row["detail"] = {"status": "可读取", "date": sample.published,
                                 "body_chars": len(sample.body), "summary_chars": len(sample.summary)}
            except Exception as exc:
                row["detail"] = {"status": "失败", "error": f"{type(exc).__name__}: {exc}"}
        row["fallbacks"] = reader.observations
    except Exception as exc:
        row.update(status="读取或解析失败", error=f"{type(exc).__name__}: {exc}")
    finally:
        reader.client.close()
    return row


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ids", default="")
    parser.add_argument("--output", default="source-probe")
    args = parser.parse_args()
    specs = yaml.safe_load((ROOT / "config/sources.yaml").read_text(encoding="utf-8"))
    existing = {s["id"] for s in specs}
    specs += [s for s in yaml.safe_load((ROOT / "config/source-candidates.yaml").read_text(encoding="utf-8")) if s["id"] not in existing]
    if args.ids:
        specs = [s for s in specs if s["id"] in args.ids.split(",")]
    rules = yaml.safe_load((ROOT / "config/keywords.yaml").read_text(encoding="utf-8"))["rules"]
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    output = ROOT / "reports" / now.date().isoformat() / args.output
    output.mkdir(parents=True, exist_ok=True)
    rows = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(probe, spec, now.date(), rules) for spec in specs]
        for future in as_completed(futures):
            row = future.result()
            rows.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
    (output / "results.json").write_text(json.dumps({"tested_at": now.isoformat(), "paid_calls": 0, "sources": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# 信源读取实测", "", f"测试时间：{now.isoformat()}；付费调用：0；不发送消息。", "", "RSS解析数量限最近30天，HTML数量是列表条目数；相关不等于最终入选。", "", "| 来源 | 状态 | 解析条目 | 有日期 | 规则相关 | 详情页 |", "|---|---|---:|---:|---:|---|"]
    for r in sorted(rows, key=lambda r: r["id"]):
        lines.append(f"| {r['name']} | {r['status']} | {r.get('parsed','—')} | {r.get('dated','—')} | {r.get('relevant','—')} | {r.get('detail',{}).get('status','—')} |")
    (output / "results.md").write_text("\n".join(lines), encoding="utf-8")
    print(str(output / "results.md"))


if __name__ == "__main__":
    main()
