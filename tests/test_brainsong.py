from datetime import date
from pathlib import Path

import httpx
import pytest

from brainsong.collect import from_search
from brainsong.editor import apply_assessment, merge, prepare, render, select, summarize
from brainsong.events import month_before, verify_events
from brainsong.model import Article, canonical
from brainsong.pipeline import load, run
from brainsong.provider import OfficialGLM, assess, CHAT, SEARCH
from brainsong.state import State
from repo_courier.feeds import business_score
from repo_courier.matching import match_rule, match_rules

ROOT = Path(__file__).resolve().parents[1]
DAY = date(2026, 9, 17)
RULE = {"id": "耳机教育", "weight": 95, "groups": [["耳机", "headphones"], ["教育", "classroom"]],
        "purpose": "只看耳机与教育交叉应用", "direct": True}


def article(n=1, **kwargs):
    return Article(f"脑电耳机新品{n}", f"https://example.org/news/{n}", DAY.isoformat(), "官网", **kwargs)


def test_groups_not_boolean_parser():
    assert match_rule("课堂教育应用耳机", RULE["groups"])
    assert not match_rule("教育政策", RULE["groups"])
    assert not match_rule("headphones sales", RULE["groups"])
    assert not match_rule("model release", [["EEG foundation model"]])
    assert match_rule("EEG foundation model", [["EEG foundation model"]])


def test_rule_paragraph_proximity():
    assert not match_rules("其他", "", "耳机销量增长。教育政策公布。", [RULE])
    assert match_rules("耳机教育应用", "", "", [RULE])


def test_zero_match_needs_ai_approval():
    item = Article("教师职称政策", "https://example.org/edu", DAY.isoformat(), "媒体")
    assert prepare(item, [RULE], [])
    assert not item.accepted and item.score == 0


def test_aliases_do_not_inflate():
    assert business_score([RULE, RULE]) == 95
    assert business_score([RULE], 70) == 85


def test_canonical_preserves_article_id():
    assert canonical("https://example.org/a?id=3&utm_source=x#y") == "https://example.org/a?id=3"
    assert canonical("https://example.org/a?id=4") != canonical("https://example.org/a?id=3")


def test_dedup_keeps_sources_not_new_funding_round():
    first, second = article(), article()
    second.url = "https://example.net/reprint"
    second.sources = [{"name": "转载", "url": second.url}]
    merged = merge([first, second])
    assert len(merged) == 1 and len(merged[0].sources) == 2
    assert len(merge([article(1), article(2)])) == 2


def test_persistent_send_state(tmp_path):
    path = tmp_path / "state.sqlite3"
    state = State(path)
    a = article()
    state.save(a)
    state.mark([a], DAY, "sent")
    state.close()
    state = State(path)
    assert state.sent(a)
    assert len(state.recent(DAY)) == 1
    state.close()


def test_score_first_seven_cap_and_two_events(tmp_path):
    state = State(tmp_path / "s.db")
    cfg, _, _, _ = load(ROOT)
    items = [article(i, score=100) for i in range(8)]
    items[-1].category, items[-1].score = "政策", 55
    picks = select(items, state, DAY, cfg)
    assert len(picks) == 4
    assert sum(a.category == '行业' for a in picks) == 3
    events = [{"name": "测试展", "date": "2026-09-20", "place": "上海", "kind": "开展", "url": "https://example.org"}]*3
    title, body = render(picks, events, DAY)
    assert title == "Brainsong Today | 2026-09-17"
    assert body.count("[展会]") == 3 and "每日简报" not in body and "推荐理由" not in body
    state.close()


def test_existing_summary_zero_summary_calls(tmp_path):
    class NoAI:
        def chat(self, *args, **kwargs):
            raise AssertionError("must not call")
    state = State(tmp_path / "s.db")
    a = article(summary="脑电耳机支持课堂中的学习状态监测。")
    summarize(a, NoAI(), state, DAY)
    assert len(a.summary) <= 50
    state.close()


def test_long_text_summary_only(tmp_path):
    class AI:
        def chat(self, task, payload, **kwargs):
            return {"summary": "脑电耳机支持教育场景的学习状态监测。"}
    state = State(tmp_path / "s.db")
    a = article(body="耳机教育研究。" * 100, matches=[RULE])
    summarize(a, AI(), state, DAY)
    assert a.summary_kind == "ai" and len(a.summary) <= 50
    state.close()


def test_ai_business_context_not_recommendation():
    a = article(matches=[RULE])
    class AI:
        def chat(self, task, payload):
            assert payload["company_profile"] == "消费级脑机教育"
            assert payload["priority_rules"][0]["purpose"] == RULE["purpose"]
            return {"items": [{"id": payload['articles'][0]['id'], "relevance": 92, "accept": True, "category": "行业", "tags": ["耳机"]}]}
    row = assess(AI(), [a], "消费级脑机教育", [RULE])[a.identity]
    apply_assessment(a, row)
    assert a.score == 93.8


def test_official_only_and_no_tools():
    calls = []
    def transport(request):
        calls.append(str(request.url))
        if str(request.url) == SEARCH:
            return httpx.Response(200, json={"search_result": []})
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok":true}'}}]})
    provider = OfficialGLM("test", client=httpx.Client(transport=httpx.MockTransport(transport)))
    provider.search("脑机接口")
    assert provider.chat("test", {}) == {"ok": True}
    assert calls == [SEARCH, CHAT]


def test_search_missing_date_not_invented():
    items = from_search([{"title": "脑机接口", "link": "https://example.org", "content": "新品"}])
    assert items[0].published == "" and not items[0].in_window(DAY, {"行业": 3})


def test_one_calendar_month_and_unverified_hidden(tmp_path):
    assert month_before(date(2026, 9, 20)) == date(2026, 8, 20)
    assert month_before(date(2026, 3, 31)) == date(2026, 2, 28)
    state = State(tmp_path / "s.db")
    events = [{"name": "展会", "date": "2026-09-20", "place": "上海", "kind": "开展", "url": "https://example.org"}]
    assert verify_events(events, DAY, None, None, state, offline=True) == []
    state.close()


def test_offline_end_to_end_no_calls(tmp_path, monkeypatch):
    import shutil
    shutil.copytree(ROOT / "config", tmp_path / "config")
    def deny(*args, **kwargs):
        raise AssertionError("network must not run in offline preview")
    monkeypatch.setattr(httpx.Client, "send", deny)
    result = run(tmp_path, DAY, offline=True, fixture=ROOT / "tests/fixtures/briefing.json")
    assert result["calls"] == {"search": 0, "chat": 0}
    assert result["sent"] is False and result["items"] <= 7
    assert "[政策]" in Path(result["report"]).read_text(encoding="utf-8")


def test_offline_send_forbidden(tmp_path):
    with pytest.raises(ValueError):
        run(ROOT, DAY, offline=True, send=True)


def test_ai_failure_logs_and_conservative_fallback(tmp_path):
    from brainsong.editor import relevance_filter
    class Failed:
        def chat(self, *args, **kwargs):
            raise ValueError("secret-should-not-appear")
    cfg, _, _, _ = load(ROOT)
    state = State(tmp_path/'s.db')
    a = article(matches=[RULE])
    unrelated = article(2, matches=[dict(RULE, direct=False)])
    picks = relevance_filter([a, unrelated], Failed(), cfg, [RULE], state, DAY)
    assert picks == []
    assert a.score == 0 and not a.accepted
    assert state.db.execute('SELECT detail FROM errors').fetchone()[0] == 'ValueError'
    state.close()


def test_ai_score_cache_invalidated_by_profile(tmp_path):
    from brainsong.editor import relevance_filter
    class AI:
        calls = 0
        def chat(self, task, payload):
            self.calls += 1
            return {'items':[{'id':row['id'],'relevance':90,'accept':True,'category':'行业','tags':['耳机']}
                             for row in payload['articles']]}
    cfg, _, _, _ = load(ROOT)
    state, ai = State(tmp_path/'s.db'), AI()
    relevance_filter([article(matches=[RULE])], ai, cfg, [RULE], state, DAY)
    relevance_filter([article(matches=[RULE])], ai, cfg, [RULE], state, DAY)
    assert ai.calls == 1
    cfg['profile'] += ' 增加新方向'
    relevance_filter([article(matches=[RULE])], ai, cfg, [RULE], state, DAY)
    assert ai.calls == 2
    state.close()


def test_no_full_text_in_database(tmp_path):
    state = State(tmp_path/'s.db')
    a = article(body='公开脑电材料。'*1000 + 'PRIVATE_FULL_TEXT_MARKER', summary='摘要')
    state.save(a)
    assert 'PRIVATE_FULL_TEXT_MARKER' not in state.db.execute('SELECT payload FROM articles').fetchone()[0]
    state.close()


def test_pending_prevents_automatic_resend(tmp_path):
    state = State(tmp_path/'s.db')
    a = article()
    state.mark([a], DAY, 'pending')
    assert state.sent(a)
    state.close()


def test_exploration_does_not_displace_policy(tmp_path):
    state = State(tmp_path/'s.db')
    cfg, _, _, _ = load(ROOT)
    state.put('counts', {'total':100,'exploration':0})
    rows = [article(i, category='政策', score=60) for i in range(7)]
    rows.append(article(7, exploration=True, score=100))
    picks = select(rows, state, DAY, cfg)
    assert sum(a.category == '政策' for a in picks) <= 3
    assert any(a.exploration for a in picks)
    state.close()


def test_date_evidence_multilingual():
    from brainsong.events import date_mentioned
    assert date_mentioned(date(2027,4,13),'13-16 April 2027, Hong Kong')
    assert date_mentioned(date(2027,4,13),'2027年4月13日至16日，香港')
    assert not date_mentioned(date(2027,4,13),'13-16 April 2026, Hong Kong')


def test_provider_refuses_model_tool_call():
    def handler(request):
        return httpx.Response(200, json={'choices':[{'message':{'tool_calls':[{}],'content':'{}'}}]})
    provider = OfficialGLM('test',client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ValueError):
        provider.chat('test',{})


def test_source_domain_filter():
    assert from_search([{'title':'EEG paper','link':'https://example.org/paper'}], '学术','arxiv.org') == []
