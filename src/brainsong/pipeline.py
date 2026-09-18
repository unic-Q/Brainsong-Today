from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from repo_courier.pushers.feishu import FeishuPusher
from .collect import Reader, bing_news, collect_source, collect_wechat, from_search, metadata
from .editor import compact_title, display_summary, usable_summary, merge, merge_event_reports, prepare, relevance_filter, render, select, summarize
from .events import verify_events
from .model import Article, digest, ranking_score, freshness_factor, policy_authority
from .provider import OfficialGLM
from .secrets import api_key
from .state import State


def load(root):
    root = Path(root)
    cfg = yaml.safe_load((root / "config/brainsong.yaml").read_text(encoding="utf-8"))
    rules = yaml.safe_load((root / "config/keywords.yaml").read_text(encoding="utf-8"))
    sources = yaml.safe_load((root / "config/sources.yaml").read_text(encoding="utf-8"))
    events = json.loads((root / "config/events.json").read_text(encoding="utf-8"))
    for rule in rules["rules"]:
        if not 1 <= rule["weight"] <= 100 or not rule["groups"] or not all(
                isinstance(g, list) and g and all(isinstance(w, str) and w.strip() for w in g) for g in rule["groups"]):
            raise ValueError("规则表格式错误")
    if not 0 <= cfg["exploration_ratio"] <= .3 or not 1 <= cfg["max_items"] <= 7:
        raise ValueError("推送配置超出范围")
    if type(cfg.get("max_academic_items", 2)) is not int or not 0 <= cfg.get("max_academic_items", 2) <= 2:
        raise ValueError("学术条数必须为0至2")
    return cfg, rules, sources, events


def queries(rules, cfg, today):
    # Short, highest-priority query in every category; no long synonym bundles.
    result = []
    for topic in cfg["search_topics"]:
        rows = sorted([r for r in rules if topic in r.get("topics", []) and not r.get("exploration")],
                      key=lambda r: -r["weight"])
        if not rows:
            continue
        # One high-priority concept per category, not an AND-like pile of synonyms.
        rule = rows[0]
        words = " ".join(g[0] for g in rule["groups"])
        intent = cfg["search_topics"][topic].split()[0]
        result.append((topic, words[:50] + " " + intent))
    explore = [r for r in rules if r.get("exploration")]
    if explore and cfg["exploration_ratio"] > 0:
        r = explore[today.toordinal() % len(explore)]
        result.append(("探索", r.get("query", r["id"])))
    return result


def run(root, today, *, offline=False, no_ai=False, send=False, fixture=None, state_path=None, resume=False, cached_only=False):
    root = Path(root)
    cfg, policy, sources, events = load(root)
    rules, exclusions = policy["rules"], policy["exclude"]
    if offline and send:
        raise ValueError("离线样例不允许推送")
    state_path = Path(state_path) if state_path else (":memory:" if offline else root / "state/brainsong.sqlite3")
    state = State(state_path)
    provider = OfficialGLM("" if offline else api_key(), cfg["ai"]["model"])
    reader = Reader()
    source_stats = []
    try:
        if not offline and not provider.key and (cfg["ai"]["enabled"] or cfg["search_enabled"]):
            raise ValueError("未配置智谱官方密钥；请设置ZHIPU_API_KEY或迁移本机密钥")
        if offline:
            if not fixture:
                raise ValueError("离线预览需要样例文件")
            values = json.loads(Path(fixture).read_text(encoding="utf-8"))
            items = [Article(**dict(v, published=v.get("published") or today.isoformat())) for v in values]
        elif resume or cached_only:
            # Explicit local continuation: no website recrawl; only configured targeted search.
            items = []
            for category, query, domain in ([] if cached_only else cfg.get('supplement_queries', [])):
                try:
                    collected = from_search(provider.search(query, domain, recency='oneMonth'), category, domain)
                    state.retain_candidates(collected, today)
                    items.extend(collected)
                    source_stats.append({'id': 'supplement:' + category, 'returned': len(collected),
                                         'dated': sum(bool(a.published) for a in collected)})
                except Exception as exc:
                    state.error(today, '定向补搜:' + category, exc)
        else:
            items = []
            for source in sources:
                if not source.get("enabled", True):
                    continue
                try:
                    if source["kind"] == "search":
                        rows = provider.search(source.get("query", "脑机接口 脑电 耳机 教育"), urlsplit(source["url"]).hostname,
                                               recency='oneMonth')
                        collected = from_search(rows, source.get("category", "行业"), urlsplit(source["url"]).hostname)
                        state.retain_candidates(collected, today)
                        items.extend(collected)
                    else:
                        collected = collect_source(source, reader, today, rules, filter_relevance=False)
                        state.retain_candidates(collected, today)
                        items.extend(collected)
                        source_stats.append({"id": source["id"], "returned": len(collected),
                                             "dated": sum(bool(a.published) for a in collected)})
                except Exception as exc:
                    state.error(today, "信源:" + source["id"], exc)
            if cfg.get("wechat_enabled", False):
                try:
                    collected = collect_wechat(root, reader, today)
                    state.retain_candidates(collected, today)
                    items.extend(collected)
                    source_stats.append({"id": "wechat", "returned": len(collected),
                                         "dated": sum(bool(a.published) for a in collected)})
                except Exception as exc:
                    state.error(today, "微信公众号", exc)
            if cfg.get("public_news_search_enabled", False):
                for topic, query in queries(rules, cfg, today):
                    if topic == "学术":
                        continue
                    try:
                        collected = bing_news(query, reader, today,
                                              {"政策": "政策", "资本": "资本"}.get(topic, "行业"))
                        state.retain_candidates(collected, today)
                        items.extend(collected)
                        source_stats.append({"id": "bing:" + topic, "returned": len(collected),
                                             "dated": sum(bool(a.published) for a in collected)})
                    except Exception as exc:
                        state.error(today, "Bing补搜:" + topic, exc)
            if cfg["search_enabled"]:
                for topic, query in queries(rules, cfg, today):
                    try:
                        domain = "arxiv.org" if topic == "学术" else ""
                        collected = from_search(provider.search(query, domain, recency='oneMonth'),
                                                {"政策": "政策", "学术": "学术", "资本": "资本"}.get(topic, "行业"), domain)
                        state.retain_candidates(collected, today)
                        items.extend(collected)
                    except Exception as exc:
                        state.error(today, "搜索:" + topic, exc)
        # First dedup before fetching or AI, then after enriching metadata.
        candidates = merge(items + state.candidates() + state.recent(today))
        filtered = []
        for item in candidates:
            if not prepare(item, rules, exclusions):
                continue
            if not offline and ((not item.published and item.matches) or
                                (item.published and not usable_summary(item.summary, item.title) and not item.body)):
                try:
                    metadata(item, reader.get(item.url))
                except Exception as exc:
                    state.error(today, "文章读取:" + item.url, exc)
            if not item.published:
                state.error(today, "文章日期:" + item.url, "missing_date")
                continue
            if not prepare(item, rules, exclusions) or not item.in_window(today, cfg["windows"]):
                continue
            if not offline:
                state.retain_candidates([item], today)
            if not state.sent(item):
                filtered.append(item)
        candidates = merge(filtered)
        candidates.sort(key=lambda a: (ranking_score(a, today), a.published), reverse=True)
        # Keep the entire candidate pool for refill; no destructive shortlist.
        pool = candidates
        policies = [a for a in pool if a.category == "政策"]
        regular = [a for a in pool if a.category != "政策" and not a.exploration]
        exploration = [a for a in pool if a.exploration and a.category != "政策"]
        # Do not let papers consume the assessment pool before industry/capital is considered.
        regular.sort(key=lambda a: (a.category == '学术', -a.score))
        candidates = policies + regular + exploration
        if cfg["ai"]["enabled"] and not offline and not no_ai:
            candidates = relevance_filter(candidates, provider, cfg, rules, state, today)
            # Previously delivered events act only as duplicate references. Once
            # merged, their sent URL aliases suppress reprints with new headlines.
            history = [a for a in state.recent(today) if a.accepted and
                       a.in_window(today, cfg['windows']) and state.sent(a)]
            candidates = merge_event_reports(history + candidates, provider, state, today)
        picks = []
        rejected = set()
        pending = select(candidates, state, today, cfg)
        while pending:
            item = pending.pop(0)
            if item.category == "学术" and sum(a.category == "学术" for a in picks) >= cfg.get("max_academic_items", 2):
                rejected.add(item.identity)
                continue
            if not offline and not usable_summary(item.summary, item.title) and len(item.body) < cfg["short_body_chars"]:
                try:
                    extras = from_search(provider.search(item.title[:70]))
                    compatible = [a for a in merge([item] + extras) if a.url == item.url]
                    if compatible:
                        item.summary, item.body, item.sources = compatible[0].summary, compatible[0].body, compatible[0].sources
                    if not usable_summary(item.summary, item.title):
                        # Broader search is not evidence of the same event. Only
                        # merged sources may supply text to this item's summary.
                        for source in item.sources[1:3]:
                            other = Article(item.title, source["url"], item.published, source["name"])
                            metadata(other, reader.get(source["url"]))
                            item.summary = item.summary or other.summary
                            item.body = max([item.body, other.body], key=len)
                except Exception as exc:
                    state.error(today, "短文补搜", exc)
            if offline or no_ai:
                from .editor import short_text
                item.summary = short_text(item.summary or item.body)
            else:
                summarize(item, provider, state, today)
            if offline or no_ai or display_summary(item.summary, item.title):
                if not offline and not no_ai:
                    compact_title(item, provider, state, today)
                picks.append(item)
            else:
                rejected.add(item.identity)
            if not pending and len(picks) < cfg["max_items"]:
                remaining = select([a for a in candidates if a.identity not in rejected], state, today, cfg)
                picked_ids = {a.identity for a in picks}
                pending = [a for a in remaining if a.identity not in picked_ids][:cfg["max_items"]-len(picks)]
        picks.sort(key=lambda a: (ranking_score(a, today), a.published), reverse=True)
        for item in pool:
            state.save(item)
        visible_events = verify_events(events, today, provider, reader, state, offline=offline or no_ai or resume or cached_only)
        title, body = render(picks, visible_events, today)
        out = root / "reports" / today.isoformat() / ("offline-preview" if offline else "live-preview")
        out.mkdir(parents=True, exist_ok=True)
        (out / "brief.md").write_text(body, encoding="utf-8")
        (out / "brief.json").write_text(json.dumps({"title": title, "offline_sample": offline,
                                                  "items": [dict(a.record(), freshness_factor=None if a.category == '政策' else freshness_factor(a, today),
                                                                 authority_factor=policy_authority(a) if a.category == '政策' else None,
                                                                 ranking_score=ranking_score(a, today)) for a in picks],
                                                  "events": visible_events}, ensure_ascii=False, indent=2), encoding="utf-8")
        delivered = False
        if send and len(picks) < cfg['max_items']:
            state.error(today, '简报未完成', 'insufficient_items_retained_for_continuation')
        if send and len(picks) == cfg['max_items']:
            import os
            webhook = os.getenv("FEISHU_WEBHOOK", "")
            parsed = urlsplit(webhook)
            if parsed.scheme != "https" or parsed.hostname != "open.feishu.cn" or not parsed.path.startswith("/open-apis/bot/v2/hook/"):
                raise ValueError("未配置有效飞书Webhook")
            # Idempotent same-day report; uncertain prior send is held, not retried.
            delivery_key = "report:" + today.isoformat() + ":" + digest(body)
            if state.get(delivery_key) not in {"pending", "sent"}:
                state.put(delivery_key, "pending")
                state.mark(picks, today, "pending")
                result = FeishuPusher(webhook).send(title, body)
                if result.success:
                    state.mark(picks, today, "sent")
                    state.put(delivery_key, "sent")
                    count = state.get("counts", {"total": 0, "exploration": 0})
                    state.put("counts", {"total": count["total"] + len(picks),
                                         "exploration": count["exploration"] + sum(a.exploration for a in picks)})
                    delivered = True
                    state.release_candidates(today)
                else:
                    state.error(today, "飞书发送", "failed_or_uncertain_requires_review")
                    raise RuntimeError("飞书发送失败或状态未知，详见本地记录")
        state.prune(today)
        logs = root / "logs"
        logs.mkdir(exist_ok=True)
        for observation in reader.observations:
            state.error(today, "采集详情:" + observation.get("source", ""), json.dumps(observation, ensure_ascii=False))
        (logs / f"{today.isoformat()}-sources.json").write_text(json.dumps(source_stats, ensure_ascii=False, indent=2), encoding="utf-8")
        (logs / f"{today.isoformat()}-ai.json").write_text(json.dumps({"calls": provider.calls, "usage": provider.usage,
                                                                   "responses": provider.diagnostics}, ensure_ascii=False, indent=2), encoding="utf-8")
        rows = state.db.execute("SELECT day,stage,detail FROM errors WHERE day=?", (today.isoformat(),)).fetchall()
        (logs / f"{today.isoformat()}.log").write_text("\n".join(" | ".join(r) for r in rows), encoding="utf-8")
        return {"report": str(out / "brief.md"), "items": len(picks), "events": len(visible_events),
                "sent": delivered, "calls": provider.calls, "errors": len(rows)}
    finally:
        logs = root / "logs"
        logs.mkdir(exist_ok=True)
        (logs / f"{today.isoformat()}-ai.json").write_text(json.dumps({"calls": provider.calls, "usage": provider.usage,
                                                                   "responses": provider.diagnostics}, ensure_ascii=False, indent=2), encoding="utf-8")
        rows = state.db.execute("SELECT day,stage,detail FROM errors WHERE day=?", (today.isoformat(),)).fetchall()
        (logs / f"{today.isoformat()}.log").write_text("\n".join(" | ".join(r) for r in rows), encoding="utf-8")
        state.close()
        reader.client.close()
        provider.client.close()
