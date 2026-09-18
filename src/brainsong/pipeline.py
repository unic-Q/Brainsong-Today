from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from repo_courier.pushers.feishu import FeishuPusher
from .collect import Reader, bing_news, collect_source, collect_wechat, from_search, metadata
from .editor import compact_title, display_summary, usable_summary, merge, merge_event_reports, prepare, relevance_filter, render, select, summarize
from .events import verify_events
from .model import Article, digest, ranking_score, freshness_factor, source_factor
from .provider import OfficialGLM
from .secrets import api_key
from .state import State
from .progress import Progress


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
    if type(cfg.get("max_academic_items", 3)) is not int or not 0 <= cfg.get("max_academic_items", 3) <= 3:
        raise ValueError("学术条数必须为0至3")
    for key, maximum in [('max_category_items', 3), ('max_company_items', 2), ('max_events', 3)]:
        value = cfg.get(key, maximum)
        if type(value) is not int or not 1 <= value <= maximum:
            raise ValueError('配额配置超出范围: ' + key)
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


def finish_item(item, cfg, state, provider, reader, today, *, offline, no_ai):
    if not offline and not usable_summary(item.summary, item.title) and len(item.body) < cfg['short_body_chars']:
        try:
            extras = from_search(provider.search(item.title[:70]))
            compatible = [a for a in merge([item] + extras) if a.url == item.url]
            if compatible:
                item.summary, item.body, item.sources = compatible[0].summary, compatible[0].body, compatible[0].sources
            if not usable_summary(item.summary, item.title):
                for source in item.sources[1:3]:
                    other = Article(item.title, source['url'], item.published, source['name'])
                    metadata(other, reader.get(source['url']))
                    item.summary = item.summary or other.summary
                    item.body = max([item.body, other.body], key=len)
        except Exception as exc:
            state.error(today, '短文补搜', exc)
    if offline or no_ai:
        from .editor import short_text
        item.summary = short_text(item.summary or item.body)
    else:
        summarize(item, provider, state, today)
    if offline or no_ai or display_summary(item.summary, item.title):
        if not offline and not no_ai:
            compact_title(item, provider, state, today)
        return True
    return False


def process_candidates(items, cfg, rules, exclusions, state, provider, reader, today,
                       progress, *, offline=False, no_ai=False):
    """Retain everything; spend enrichment and AI work only on priority batches."""
    pool = merge(items)
    queue = []
    for item in pool:
        if not prepare(item, rules, exclusions) or state.sent(item):
            continue
        # Dates already known to be outside the window need no page request.
        if item.published and not item.in_window(today, cfg['windows']):
            continue
        queue.append(item)
    scoring = cfg.get('scoring', {})
    def estimate(item):
        import re
        weight = max((r['weight'] for r in item.matches), default=20)
        signal = bool(re.search('发布|新品|融资|收购|标准|突破|launch|funding', item.title, re.I))
        return (weight + (20 if signal else 0)) * source_factor(item, scoring) * (freshness_factor(item, today, scoring) if item.published else .8)
    queue.sort(key=lambda a: (a.exploration, -estimate(a),
                             a.category == '学术', a.category != '政策', a.identity))
    counts = state.get('counts', {'total': 0, 'exploration': 0})
    allowance = int((counts['total'] + cfg['max_items']) * cfg['exploration_ratio']) - counts['exploration']
    if allowance > 0:
        # Give the due exploration slot a chance before the early stop. It
        # still requires relevance approval and never becomes a forced filler.
        discovery = next((a for a in queue if a.exploration), None)
        if discovery is not None:
            queue.remove(discovery)
            queue.insert(0, discovery)
    progress('candidate queue', retained=len(pool), eligible=len(queue))
    ai = cfg['ai']['enabled'] and not offline and not no_ai
    history = [a for a in state.recent(today) if a.accepted and
               a.in_window(today, cfg['windows']) and state.sent(a)] if ai else []
    accepted, picks, rejected = [], [], set()
    batch_size = min(6, max(1, cfg['ai']['batch_size']))
    cursor = extra_batches = metadata_reads = 0
    budget = cfg.get('max_analysis_candidates', 36)
    while cursor < min(len(queue), budget):
        if len(picks) >= cfg['max_items']:
            promising = [a for a in queue[cursor:] if estimate(a) > min(ranking_score(p, today, scoring) for p in picks) * 1.1]
            if not promising or extra_batches >= cfg.get('max_extra_batches', 1):
                break
            tail = queue[cursor:]
            queue[cursor:] = promising + [a for a in tail if a not in promising]
            extra_batches += 1
            progress('priority check extra batch', candidates=len(promising))
        raw = queue[cursor:min(cursor+batch_size, budget)]
        cursor += len(raw)
        progress('batch start', processed=cursor, total=len(queue), ready=len(picks))
        batch = []
        for offset, item in enumerate(raw, 1):
            if not offline and ((not item.published and item.matches) or
                                (item.published and not usable_summary(item.summary, item.title) and not item.body)):
                if metadata_reads >= cfg.get('max_metadata_reads', 12):
                    progress('article deferred metadata budget')
                    continue
                metadata_reads += 1
                progress('article read', article=cursor-len(raw)+offset)
                try:
                    metadata(item, reader.get(item.url))
                except Exception as exc:
                    state.error(today, '文章读取:' + item.url, exc)
            if not offline:
                state.retain_candidates([item], today)
            if not item.published:
                state.error(today, '文章日期:' + item.url, 'missing_date')
                continue
            if prepare(item, rules, exclusions) and item.in_window(today, cfg['windows']):
                batch.append(item)
        accepted.extend(relevance_filter(batch, provider, cfg, rules, state, today) if ai
                        else [a for a in batch if a.accepted])
        if ai and batch:
            accepted = merge_event_reports(history + accepted, provider, state, today)
            # History is only a duplicate reference, not a new output candidate.
            accepted = [a for a in accepted if not state.sent(a)]
        # Re-rank all assessed candidates, reusing valid summaries from earlier batches.
        picks = []
        while True:
            selected = select([a for a in accepted if a.identity not in rejected], state, today, cfg)
            if not selected:
                picks = []
                break
            picks, failed = [], False
            for item in selected:
                progress('summary start', ready=len(picks))
                if finish_item(item, cfg, state, provider, reader, today,
                               offline=offline, no_ai=no_ai or not cfg['ai']['enabled']):
                    picks.append(item)
                    state.save(item)
                else:
                    rejected.add(item.identity)
                    failed = True
            if not failed:
                break
        # Preserve every completed score even if a later batch is cancelled.
        for item in batch:
            state.save(item)
        progress('batch complete', processed=cursor, accepted=len(accepted), ready=len(picks))
    progress('selection complete', processed=cursor, deferred=len(queue)-cursor, ready=len(picks))
    return pool, picks


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
    progress = Progress(root / 'logs' / f'{today}-progress.log')
    state.progress = provider.progress = progress
    progress('run start', cached_only=cached_only, resume=resume, send=send)
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
            site_searches = 0
            # Within the paid site-search budget, official policy entries lead.
            ordered_sources = sorted(sources, key=lambda s: 0 if (urlsplit(s.get('url', '')).hostname or '').endswith('.gov.cn') else 1)
            for source in ordered_sources:
                if not source.get("enabled", True):
                    continue
                progress('source start ' + source['id'])
                try:
                    if source["kind"] == "search":
                        if site_searches >= cfg.get('site_search_limit', 3):
                            progress('site search deferred ' + source['id'])
                            continue
                        site_searches += 1
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
                progress('source complete ' + source['id'], candidates=len(items))
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
        pool, picks = process_candidates(items + state.candidates() + state.recent(today),
                                         cfg, rules, exclusions, state, provider, reader, today,
                                         progress, offline=offline, no_ai=no_ai)
        picks.sort(key=lambda a: (ranking_score(a, today, cfg.get('scoring')), a.published), reverse=True)
        for item in pool:
            state.save(item)
        visible_events = verify_events(events, today, provider, reader, state, offline=offline or no_ai or resume or cached_only)
        visible_events = visible_events[:cfg.get('max_events', 3)]
        progress('render', items=len(picks), events=len(visible_events))
        title, body = render(picks, visible_events, today)
        out = root / "reports" / today.isoformat() / ("offline-preview" if offline else "live-preview")
        out.mkdir(parents=True, exist_ok=True)
        (out / "brief.md").write_text(body, encoding="utf-8")
        (out / "brief.json").write_text(json.dumps({"title": title, "offline_sample": offline,
                                                  "items": [dict(a.record(), freshness_factor=freshness_factor(a, today, cfg.get('scoring')),
                                                                 source_factor=source_factor(a, cfg.get('scoring')),
                                                                 ranking_score=ranking_score(a, today, cfg.get('scoring'))) for a in picks],
                                                  "events": visible_events}, ensure_ascii=False, indent=2), encoding="utf-8")
        delivered = False
        if send and not picks and not visible_events:
            state.error(today, '无可发送内容', 'no_qualified_items')
        if send and (picks or visible_events):
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
                progress('Feishu send start', items=len(picks))
                result = FeishuPusher(webhook).send(title, body)
                if result.success:
                    state.mark(picks, today, "sent")
                    state.put(delivery_key, "sent")
                    count = state.get("counts", {"total": 0, "exploration": 0})
                    state.put("counts", {"total": count["total"] + len(picks),
                                         "exploration": count["exploration"] + sum(a.exploration for a in picks)})
                    delivered = True
                    state.release_candidates(today)
                    progress('Feishu send complete', items=len(picks))
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
