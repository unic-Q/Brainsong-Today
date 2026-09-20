from __future__ import annotations

import re
from difflib import SequenceMatcher

from repo_courier.feeds import business_score
from repo_courier.matching import contains, match_rules, normalize
from .model import digest, title_key, dictionary_result, ranking_score, shortlist_score, company_keys, star_text
from .provider import QualityError, assess


def category_hint(item):
    text = item.title + " " + item.summary[:300]
    if item.category == "政策" or re.search(r"(发布|印发|公布|征求意见).{0,30}(条例|规定|办法|标准|指南)|监管政策|国家标准", text):
        return "政策"
    if item.category == "学术" or "arxiv.org" in item.url:
        return "学术"
    if re.search(r"融资|并购|收购|天使轮|种子轮|[ABCDEF]轮|funding|raises|acquisition", text, re.I):
        return "资本"
    if re.search(r'论文|研究团队|研究者|实验组|试验|准确率|解码|模型|dataset|decoding|accuracy|paper|study', text, re.I) and not re.search(r'融资|新品|SDK|产品发布|版本更新|release|launch|studio', text, re.I):
        return "学术"
    return "行业"


def prepare(item, rules, exclusions):
    if not item.url or dictionary_result(item.url):
        return False
    text = normalize(item.title + " " + item.summary)
    if any(contains(text, normalize(x)) for x in exclusions):
        return False
    item.matches = match_rules(item.title, item.summary, item.body, rules)
    # Zero keyword match is not proof of irrelevance. Require AI approval instead
    # of discarding emerging companies or technical SDKs before assessment.
    item.accepted = bool(item.matches)
    item.category = category_hint(item)
    item.exploration = bool(item.matches) and all(r.get("exploration", False) for r in item.matches)
    item.tags = list(dict.fromkeys(r["groups"][0][0] for r in sorted(item.matches, key=lambda r: -r["weight"])))[:3]
    item.score = business_score(item.matches)
    return True


def same_event(a, b):
    if {s['url'] for s in a.sources} & {s['url'] for s in b.sources}:
        return True
    if a.url == b.url or title_key(a.title) == title_key(b.title):
        return True
    # Conservative near-duplicate collapse: same day, nearly identical headlines,
    # all explicit numeric facts identical. Different financing rounds remain separate.
    return (a.published == b.published and bool(a.published)
            and len(title_key(a.title)) >= 14
            and re.findall(r"\d+(?:\.\d+)?", a.title) == re.findall(r"\d+(?:\.\d+)?", b.title)
            and SequenceMatcher(None, title_key(a.title), title_key(b.title)).ratio() >= .92)


def merge(items):
    output = []
    for item in items:
        item.capture_source()
        if item.summary_kind in {'ai', 'failed', 'formatted'}:
            item.summary = item.source_summary
            item.summary_kind = 'source'
            item.summary_version = ''
        existing = next((a for a in output if same_event(a, item)), None)
        if existing is None:
            output.append(item)
            continue
        urls = {s["url"] for s in existing.sources}
        existing.sources.extend(s for s in item.sources if s["url"] not in urls)
        dates = [d for d in (existing.first_reported, existing.published, item.first_reported, item.published) if d]
        existing.first_reported = min(dates, default='')
        if len(item.summary) > len(existing.summary):
            existing.summary = item.summary
            existing.summary_kind = item.summary_kind
            existing.source_summary = item.source_summary
        if len(item.body) > len(existing.body):
            existing.body = item.body
            existing.source_excerpt = item.source_excerpt
        if not existing.published:
            existing.published = item.published
        # For an already matched policy event, keep its original official entry
        # as the main link while retaining the other report's source evidence.
        from urllib.parse import urlsplit
        if item.category == '政策' and (urlsplit(item.url).hostname or '').endswith('.gov.cn') and not (urlsplit(existing.url).hostname or '').endswith('.gov.cn'):
            existing.url, existing.title, existing.source = item.url, item.title, item.source
            existing.published = item.published or existing.published
            existing.category = '政策'
    return output


def relevance_filter(items, provider, cfg, rules, state, today):
    # Cache includes profile and rule content: changing preferences invalidates scores.
    import json
    profile = cfg['profile']
    if cfg.get('relevance_examples'):
        profile += '\n用户最新相关性评分锚点（优先于通用偏好；不是已发生的新闻）：' + json.dumps(cfg['relevance_examples'], ensure_ascii=False)
    signature = digest(json.dumps(["assessment-v6-calibrated", profile, rules], ensure_ascii=False, sort_keys=True))
    pending = []
    for item in items:
        evidence_hash = digest(item.title + (item.summary or item.body)[:700])
        cache_key = "ai:" + item.identity
        cached = state.get(cache_key)
        if cached and cached["signature"] == signature and cached["evidence"] == evidence_hash:
            row = cached["row"]
            apply_assessment(item, row)
        else:
            pending.append((item, cache_key, evidence_hash))
    batch_size = min(6, cfg["ai"]["batch_size"])
    for start in range(0, len(pending), batch_size):
        batch = pending[start:start+batch_size]
        queue = [(batch, 0)]
        while queue:
            remaining, attempt = queue.pop(0)
            try:
                rows = assess(provider, [x[0] for x in remaining], profile, rules,
                              evidence_limit=700 if attempt == 0 else 350)
                if rows.issues:
                    state.error(today, "AI相关性筛选", QualityError("/".join(rows.issues), expected=len(remaining), valid=len(rows), attempt=attempt+1))
            except Exception as exc:
                state.error(today, "AI相关性筛选", exc)
                rows = {}
            failed = []
            for item, key, evidence in remaining:
                if item.identity not in rows:
                    item.accepted, item.relevance, item.score = False, None, 0
                    failed.append((item, key, evidence))
                    continue
                apply_assessment(item, rows[item.identity])
                state.put(key, {"signature": signature, "evidence": evidence,
                                "row": rows[item.identity], "day": today.isoformat()})
            # Retry only failures. Smaller batches and clipped INPUT evidence
            # avoid repeating the same oversized request or parsing broken JSON.
            if failed and attempt == 0:
                size = max(1, len(failed) // 2)
                queue.extend((failed[i:i+size], 1) for i in range(0, len(failed), size))
    return [a for a in items if a.accepted]


def apply_assessment(item, row):
    item.relevance = row["relevance"]
    item.accepted = row["accept"] and row["relevance"] >= 60
    item.category = row["category"]
    item.tags = [re.sub(r"[\[\]<>\n]", "", t)[:18] for t in row["tags"][:3]]
    item.score = business_score(item.matches, item.relevance)
    evidence = item.title + ' ' + (item.summary or item.body)[:700]
    quote = row.get('event_evidence', '')
    event = row.get('event_type', 'ordinary')
    item.event_type = event if event in {'policy', 'product', 'breakthrough', 'funding'} and isinstance(quote, str) and len(quote) >= 8 and quote in evidence else 'ordinary'
    kind = row.get('source_kind', 'secondary')
    item.source_kind = kind if kind in {'original', 'media', 'secondary', 'unknown'} else 'unknown'
    names = row.get('companies', [])
    item.companies = [n for n in names if isinstance(n, str) and len(n.strip()) >= 2 and n not in {'公司', '企业', '团队', '研究团队'} and n.casefold() in evidence.casefold()][:3] if isinstance(names, list) else []
    subject = row.get('subject', '')
    item.subject = subject.strip() if isinstance(subject, str) and 2 <= len(subject.strip()) <= 80 and subject.strip() in evidence else ''
    if item.source_kind == 'unknown':
        item.accepted = False


def merge_event_reports(items, provider, state, today):
    """Conservative AI duplicate grouping, using only already-stored short evidence."""
    import json
    rows = [a for a in items if a.category != '学术' and a.accepted]
    if len(rows) < 2:
        return items
    payload = [{'id': f'A{i}', 'title': a.title, 'date': a.published,
                'category': a.category, 'summary': a.summary[:240]} for i, a in enumerate(rows)]
    key = 'dedup:' + digest(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    cached = state.get(key)
    try:
        if cached:
            groups = cached['groups']
        else:
            result = provider.chat('识别同一事件的重复报道。仅合并有明确相同主体、同一次发布/同一标准/同一轮融资的报道；'
                                   '参与起草、科普解读如果仍围绕同一次标准发布，也合并。不同省政策、不同轮融资、不同产品不可合并。'
                                   '不因主题类似就合并。不确定就保留。只返回 {"groups":[["A0","A2"]]}，只列重复组，每个ID最多一次。', payload)
            groups = result.get('groups')
            if not isinstance(groups, list):
                raise QualityError('dedup_invalid')
        by_id = {p['id']: a for p, a in zip(payload, rows)}
        seen, valid = set(), []
        from datetime import date
        for group in groups:
            if not isinstance(group, list) or len(group) < 2 or any(not isinstance(k, str) or k not in by_id or k in seen for k in group) or len(set(group)) != len(group):
                raise QualityError('dedup_invalid_ids')
            members = [by_id[k] for k in group]
            # A model's grouping must never erase distinct financing rounds.
            rounds = {tuple(sorted(set(m.upper() for m in re.findall(
                r'(?<![A-Za-z])([A-F](?:\+)?)(?:轮|\s+round)', a.title, re.I)))) for a in members}
            rounds.discard(())
            if len(rounds) > 1:
                continue
            days = [date.fromisoformat(a.published[:10]) for a in members]
            if len({a.category for a in members}) != 1 or (max(days)-min(days)).days > 7:
                continue
            seen.update(group)
            valid.append(members)
        state.put(key, {'groups': groups, 'day': str(today)})
        removed = set()
        for members in valid:
            from urllib.parse import urlsplit
            primary = sorted(members, key=lambda a: ((urlsplit(a.url).hostname or '').endswith('.gov.cn'),
                                                     ranking_score(a, today)), reverse=True)[0]
            urls = {s['url'] for s in primary.sources}
            primary.first_reported = min(a.first_reported or a.published for a in members)
            for other in members:
                if other is primary:
                    continue
                removed.add(other.identity)
                for source in other.sources:
                    if source['url'] not in urls:
                        primary.sources.append(source)
                        urls.add(source['url'])
        return [a for a in items if a.identity not in removed]
    except Exception as exc:
        state.error(today, '事件合并', exc)
        return items


def select(items, state, today, cfg):
    eligible = [a for a in items if a.accepted and a.in_window(today, cfg["windows"]) and not state.sent(a)]
    scoring = cfg.get('scoring', {})
    eligible = [a for a in eligible if shortlist_score(a, today, scoring) > 0 and ranking_score(a, today, scoring) > 0]
    eligible.sort(key=lambda a: ((a.first_reported or a.published)[:10] if cfg.get('acquisition', {}).get('recency_first') else '',
                                shortlist_score(a, today, scoring), a.published, a.identity), reverse=True)
    cap = min(7, cfg["max_items"])
    category_cap = cfg.get('max_category_items', 3)
    def fits(item, chosen, relaxed=False):
        limit = min(category_cap, cfg.get('max_academic_items', 3)) if item.category == '学术' else category_cap
        limit += int(relaxed)
        return (sum(a.category == item.category for a in chosen) < limit and
                all(sum(key in company_keys(a, scoring) for a in chosen) < cfg.get('max_company_items', 2) + int(relaxed)
                    for key in company_keys(item, scoring)))
    # First stage: relevance × age, with a soft diversity limit (4/direction,
    # 3/company) so the ten-item pool is not monopolized by one actor/topic.
    limit = cfg.get('shortlist_items', 10)
    shortlist = []
    for item in eligible:
        if fits(item, shortlist, relaxed=True):
            shortlist.append(item)
        if len(shortlist) == limit:
            break
    for item in eligible:
        if len(shortlist) >= limit:
            break
        if item not in shortlist:
            shortlist.append(item)
    def final_selection():
        ordered = sorted(shortlist, key=lambda a: (ranking_score(a, today, scoring), shortlist_score(a, today, scoring), a.identity), reverse=True)
        result = []
        for item in ordered:
            if fits(item, result):
                item.recommendation_score = ranking_score(item, today, scoring)
                result.append(item)
            if len(result) == cap:
                break
        return result
    picks = final_selection()
    for item in eligible:
        if len(picks) >= cap:
            break
        if item not in shortlist:
            shortlist.append(item)
            picks = final_selection()
    return picks


def short_text(text, limit=100):
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    sentences = re.split(r"(?<=[。！？!?])\s*|(?<!\d)\.(?!\d)\s*", text)
    chosen = ""
    for sentence in sentences:
        if len(chosen + sentence) <= limit:
            chosen += sentence
        else:
            break
    # Mark a source extract as an extract, not an invented full summary.
    return chosen or text[:limit-1].rstrip() + "…"


def usable_summary(text, title=""):
    text = re.sub(r"\s+", " ", text or "").strip()
    return (len(text) >= 12 and text != title.strip()
            and len(re.findall(r"[A-Za-z\u4e00-\u9fff]", text)) >= 8
            and text.casefold() not in {"doc", "pdf", "read more", "click here"}
            and not text.startswith('Abstract page for arXiv paper')
            and not re.match(r"^[\d.+\-$]", text))


def display_summary(text, title=""):
    return usable_summary(text, title) and len(text) <= 100 and len(re.findall(r"[\u4e00-\u9fff]", text)) >= 6 and not text.endswith(("…", "..."))


def summarize(item, provider, state, today):
    version = '100-v5-subject-first'
    item.capture_source()
    def subject_first(text):
        return item.subject + '：' + text if item.subject and not text.startswith(item.subject) else text
    if display_summary(item.summary, item.title) and (item.summary_kind != 'ai' or item.summary_version == version):
        front = subject_first(item.summary)
        if display_summary(front, item.title):
            if front != item.summary and item.summary_kind != 'ai':
                item.summary_kind = 'formatted'
            item.summary = front
            return
    source_summary = item.source_summary if usable_summary(item.source_summary, item.title) else ""
    paragraphs = (item.body or item.source_excerpt).splitlines()
    evidence = source_summary[:6000] or "\n".join(paragraphs[:4] + [p for p in paragraphs[4:] if match_rules("", "", p, item.matches)])[:6000]
    signature = digest(item.title + evidence + item.subject)
    cache_key = "summary-v5:" + item.identity
    cached = state.get(cache_key)
    if cached and cached.get("evidence") == signature and display_summary(cached.get("summary", ""), item.title):
        item.summary = cached["summary"]
        item.summary_kind = "ai"
        item.summary_version = version
        return
    if not usable_summary(evidence, item.title):
        item.summary = ""
        state.error(today, "摘要", QualityError("summary_insufficient_evidence"))
        return
    try:
        value = provider.chat('只根据原始材料写中文事实摘要，摘要单独计算，含标点不超过100个字符；标题、日期、标签、来源与链接不计入。材料充分时目标70至100字。必须以subject指定的事件主体开头，再写动作和结果；不是以报道媒体开头。论文有机构/教授团队就写清；没有明确姓名不得猜测。subject为空时只使用证据中明确的主体。材料不足可更短，严禁扩写无依据的事实。保留重要数字和不确定性。区分报道日期与事件日期，历史政策解读须注明解读或回顾，不得写成刚发布，不随意使用今日。不要理由、评分或链接。返回 {"summary":"..."}。',
                              {"title": item.title, "evidence": evidence, 'subject': item.subject})
        summary = value.get("summary")
        if isinstance(summary, str):
            summary = subject_first(summary.strip())
        if isinstance(summary, str) and len(summary.strip()) > 100:
            state.error(today, '摘要压缩重试', QualityError('summary_over_limit', characters=len(summary.strip())))
            value = provider.chat('根据原始证据压缩草稿。只保留证据支持的核心事实，不添加或推测。'
                                  '中文摘要含标点70至100字符，材料不足可更短。删去次要细节以确保不超过100字符。'
                                  '以subject事件主体开头，不猜测姓名。只返回 {"summary":"..."}。',
                                  {'title': item.title, 'evidence': evidence, 'draft': summary[:1000], 'subject': item.subject})
            summary = value.get('summary')
            if isinstance(summary, str):
                summary = subject_first(summary.strip())
        if not isinstance(summary, str) or not display_summary(summary.strip(), item.title):
            raise QualityError("summary_invalid_or_over_100")
        item.summary = summary.strip()
        item.summary_kind = "ai"
        item.summary_version = version
        state.put(cache_key, {"summary": item.summary, "version": version, "evidence": signature, "day": today.isoformat()})
    except Exception as exc:
        state.error(today, "摘要", exc)
        item.summary = ""
        item.summary_kind = "failed"


def compact_title(item, provider, state, today):
    """Rewrite long display headlines; preserve original titles for dedup/history.

    40 characters only triggers editing. It is not an output length limit and
    never shares the summary's 100-character budget.
    """
    if len(item.title) <= 40:
        item.display_title = item.title
        return
    key = "headline-v1:" + digest(item.title)
    cached = state.get(key)
    if cached and cached.get("title"):
        item.display_title = cached["title"]
        return
    try:
        value = provider.chat(
            '将过长新闻标题改写为精炼、完整的中文标题，不按固定字数截断。保留主体、核心事件及必要限定，'
            '不得新增事实、夸大效果或把历史研究写成新突破。去掉公文套话和不必要的编号。'
            '标题独立于摘要，100字限制仅用于摘要，不是标题加摘要。返回 {"title":"精简标题"}。',
            {"original_title": item.title, "summary": item.summary})
        title = value.get("title")
        if not isinstance(title, str) or not title.strip() or len(title.strip()) >= len(item.title) or '\n' in title or title.endswith(('…', '...')):
            raise QualityError("headline_invalid")
        item.display_title = title.strip()
        state.put(key, {"title": item.display_title, "day": today.isoformat()})
    except Exception as exc:
        item.display_title = item.title
        state.error(today, "标题精简", exc)


def escape(text):
    return re.sub(r"([\\`*_\[\]<>])", r"\\\1", str(text)).replace("\n", " ")


def render(items, events, today):
    title = f"Brainsong Today | {today.isoformat()}"
    lines = [title, ""]
    for n, item in enumerate(items[:7], 1):
        tags = "／".join(escape(t) for t in item.tags[:3])
        flag = " · 探索" if item.exploration else ""
        names = list(dict.fromkeys(s["name"] for s in item.sources))
        lines += [f"**{n}. [{item.category}] {escape(item.display_title or item.title)}**",
                  '推荐指数：' + star_text(item.recommendation_score if item.recommendation_score is not None else ranking_score(item, today)),
                  f"{item.published[:10]} · {tags}{flag}", escape(short_text(item.summary)),
                  f"（来源：{'、'.join(escape(n) for n in names[:5])}）[原文]({item.url.replace(')', '%29').replace('(', '%28')})", ""]
    for event in events[:3]:
        lines += [f"[展会] {escape(event['name'])}｜{event['date']}｜{escape(event['place'])}｜{escape(event['kind'])}",
                  f"[原文]({event['url']})", ""]
    body = "\n".join(lines).strip()
    if len(body) > 18000:
        raise ValueError("简报超长，禁止静默截断")
    return title, body
