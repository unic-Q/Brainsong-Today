from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from repo_courier.pushers.feishu import FeishuPusher
from .collect import Reader, bing_news, collect_source, collect_wechat, from_search, metadata
from .editor import compact_title, display_summary, usable_summary, merge, merge_event_reports, prepare, relevance_filter, render, select, summarize, fill_link_only
from .events import verify_events
from .model import Article, digest, ranking_score, shortlist_score, freshness_factor, source_factor, star_text
from .provider import OfficialGLM
from .secrets import api_key
from .state import State
from .progress import Progress
from .acquisition import business_context, source_priority, date_priority, estimated_relevance


def load(root):
    root = Path(root)
    cfg = yaml.safe_load((root / "config/brainsong.yaml").read_text(encoding="utf-8"))
    rules = yaml.safe_load((root / "config/keywords.yaml").read_text(encoding="utf-8"))
    sources = yaml.safe_load((root / "config/sources.yaml").read_text(encoding="utf-8"))
    events = json.loads((root / "config/events.json").read_text(encoding="utf-8"))
    for example in cfg.get('relevance_examples', []):
        if not isinstance(example.get('text'), str) or type(example.get('score')) is not int or not 0 <= example['score'] <= 100:
            raise ValueError('相关性评分示例格式错误')
    for hint in cfg.get('acquisition', {}).get('priority_hints', []):
        import re
        if type(hint.get('score')) is not int or not 0 <= hint['score'] <= 100 or not hint.get('all'):
            raise ValueError('采集优先级规则错误')
        for pattern in hint['all']:
            re.compile(pattern)
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
    replacements = cfg.get('max_replacement_candidates_per_category', 1)
    if type(replacements) is not int or not 0 <= replacements <= 2:
        raise ValueError('每类替换分析名额必须为0至2')
    margin = cfg.get('replacement_margin', 1.25)
    if not isinstance(margin, (int, float)) or not 1 < margin <= 2:
        raise ValueError('替换候选优势系数必须大于1且不超过2')
    if cfg.get('shortlist_items', 10) != 10:
        raise ValueError('当前两轮方案入围池必须为10条')
    if cfg.get('policy_scope') != 'china':
        raise ValueError('政策范围必须限定为中国国内')
    scoring = cfg.get('scoring', {})
    category_weights = scoring.get('category_weights', {})
    if set(category_weights) != {'行业', '资本', '政策', '学术'} or any(
            type(value) not in (int, float) or not 1 <= value <= 1.5
            for value in category_weights.values()):
        raise ValueError('分类业务权重必须完整，且为1到1.5')
    weights = scoring.get('source_weights', {'authority': .7, 'recognition': .3})
    if set(weights) != {'authority', 'recognition'} or any(not isinstance(v, (int, float)) or not 0 <= v <= 1 for v in weights.values()) or abs(sum(weights.values())-1) > .000001:
        raise ValueError('来源权重必须非负且合计1')
    for values in list(scoring.get('source_domains', {}).values()) + list(scoring.get('source_reputation', {}).values()):
        if not isinstance(values, list) or len(values) != 2 or any(not isinstance(v,(int,float)) or not 0 <= v <= 100 for v in values):
            raise ValueError('来源权威性/知名度须为0到100')
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


def direction_batch(queue, picks, cfg, estimate, size, can_read=True, exploration_due=False,
                    analysis_counts=None, replacement_counts=None):
    """Recent business-relevant evidence first; convenience cannot outrank value."""
    analysis_counts = analysis_counts if analysis_counts is not None else Counter()
    replacement_counts = replacement_counts if replacement_counts is not None else Counter()
    def needs_read(a):
        return not a.published or (not usable_summary(a.summary, a.title) and not a.body)
    def full(a):
        limit = min(cfg.get('max_category_items', 3), cfg.get('max_academic_items', 3)) if a.category == '学术' else cfg.get('max_category_items', 3)
        return sum(p.category == a.category for p in picks) >= limit
    candidates = []
    for a in queue:
        if needs_read(a) and not can_read:
            continue
        if full(a):
            current = [p for p in picks if p.category == a.category]
            # Only clearly stronger, dated candidates can spend work replacing
            # a full direction; unknown dates cannot claim to be fresher.
            if (replacement_counts[a.category] >= cfg.get('max_replacement_candidates_per_category', 1)
                    or not current or not a.published
                    or estimate(a) <= min(estimate(p) for p in current) * cfg.get('replacement_margin', 1.25)):
                continue
        candidates.append(a)
    result = []
    while candidates and len(result) < size:
        available = [a for a in candidates if not full(a) or
                     replacement_counts[a.category] + sum(full(x) and x.category == a.category for x in result)
                     < cfg.get('max_replacement_candidates_per_category', 1)]
        if not available:
            break
        def priority(a):
            evidence_level = 2 if not a.published else 1 if needs_read(a) else 0
            representation = sum(p.category == a.category for p in picks + result)
            recent = date_priority(a) if cfg.get('acquisition', {}).get('recency_first') else 0
            # Spend scarce AI slots across directions before deepening one rich
            # source pool. A full direction can only use its explicit replacement slot.
            return (full(a), analysis_counts[a.category] + sum(x.category == a.category for x in result),
                    not (a.exploration and exploration_due),
                    estimated_relevance(a, cfg) < 60,
                    -recent,
                    -estimate(a), -source_priority(a, cfg), representation, evidence_level, a.identity)
        chosen = min(available, key=priority)
        candidates.remove(chosen)
        result.append(chosen)
    for item in result:
        analysis_counts[item.category] += 1
        if full(item):
            replacement_counts[item.category] += 1
    return result


def process_candidates(items, cfg, rules, exclusions, state, provider, reader, today,
                       progress, *, offline=False, no_ai=False):
    """Retain everything; spend enrichment and AI work only on priority batches."""
    pool = merge([a for a in items if not state.deleted(a)])
    queue = []
    for item in pool:
        if not prepare(item, rules, exclusions) or state.sent(item) or not business_context(item, cfg):
            continue
        # Dates already known to be outside the window need no page request.
        if item.published and not item.in_window(today, cfg['windows']):
            continue
        queue.append(item)
    scoring = cfg.get('scoring', {})
    def estimate(item):
        weight = estimated_relevance(item, cfg)
        return weight * (freshness_factor(item, today, scoring) if item.published else 0)
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
    cursor = analysed = extra_batches = metadata_reads = 0
    analysis_counts, replacement_counts = Counter(), Counter()
    remaining = list(queue)
    budget = cfg.get('max_analysis_candidates', 36)
    while remaining and analysed < budget:
        qualified = [a for a in accepted if a.identity not in rejected]
        have_ten = len(qualified) >= cfg.get('shortlist_items', 10)
        if len(picks) >= cfg['max_items'] and have_ten:
            cutoff = sorted((shortlist_score(a, today, scoring) for a in qualified), reverse=True)[9]
            promising = [a for a in remaining if a.published and estimate(a) > cutoff * 1.1]
            if not promising or extra_batches >= cfg.get('max_extra_batches', 1):
                break
            extra_batches += 1
            progress('priority check extra batch', candidates=len(promising))
            available = promising
        else:
            available = remaining
        raw = direction_batch(available, picks, cfg, estimate, min(batch_size, budget-analysed),
                              can_read=offline or metadata_reads < cfg.get('max_metadata_reads', 12),
                              exploration_due=allowance > 0 and not any(a.exploration for a in picks),
                              analysis_counts=analysis_counts, replacement_counts=replacement_counts)
        if not raw:
            progress('no eligible refill within evidence and quota limits', ready=len(picks))
            break
        chosen_ids = {a.identity for a in raw}
        remaining = [a for a in remaining if a.identity not in chosen_ids]
        cursor += len(raw)
        progress('batch start', inspected=cursor, analysed=analysed, total=len(queue), ready=len(picks))
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
            if prepare(item, rules, exclusions) and business_context(item, cfg) and item.in_window(today, cfg['windows']):
                batch.append(item)
        analysed += len(batch)
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
        progress('batch complete', inspected=cursor, analysed=analysed, accepted=len(accepted), ready=len(picks))
        for category in ('行业', '资本', '政策', '学术'):
            progress('direction ' + category, ready=sum(a.category == category for a in picks),
                     remaining_ready=sum(a.category == category and bool(a.published) and
                                         (usable_summary(a.summary, a.title) or bool(a.body)) for a in remaining))
    picks = fill_link_only(picks, [a for a in accepted if a.identity in rejected], state, today, cfg)
    progress('selection complete', inspected=cursor, analysed=analysed, deferred=len(remaining), ready=len(picks),
             analysed_mix=','.join(f'{k}:{analysis_counts[k]}' for k in ('行业','资本','政策','学术')))
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
        picks.sort(key=lambda a: (ranking_score(a, today, cfg.get('scoring')), shortlist_score(a, today, cfg.get('scoring')), a.identity), reverse=True)
        for item in picks:
            item.recommendation_score = ranking_score(item, today, cfg.get('scoring'))
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
                                                                 shortlist_score=shortlist_score(a, today, cfg.get('scoring')),
                                                                 stars=star_text(a.recommendation_score),
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
                    if os.getenv('DAILY_DELIVERY') == 'true':
                        state.put('daily-delivery:' + today.isoformat(), 'sent')
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
