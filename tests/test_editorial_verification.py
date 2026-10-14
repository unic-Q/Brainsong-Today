from datetime import date, timedelta

from brainsong import pipeline
from brainsong.authority import attach
from brainsong.editor import render
from brainsong.model import Article, source_factor
from brainsong.repeat_guard import policy_document, previously_covered
from brainsong.state import State
from brainsong.verification import verify_finalist


DAY = date(2026, 10, 12)


def test_same_policy_document_survives_reworded_title_and_longer_history(tmp_path):
    state = State(tmp_path / 'history.sqlite3')
    old = Article('中共中央 国务院关于发展新质生产力的意见',
                  'https://www.gov.cn/zhengce/example', '2026-10-09', '中国政府网',
                  category='政策')
    new = Article('中共中央、国务院印发《关于发展新质生产力的意见》',
                  'https://media.example/news', '2026-10-12', '媒体', category='政策')
    state.record_sent_events([old], date(2026, 10, 9))
    history = state.recent_sent_events(DAY, 60)
    assert policy_document(old) == policy_document(new)
    assert previously_covered(new, history, object(), state, DAY)
    state.close()


def test_same_company_reframing_is_checked_once_and_cached(tmp_path):
    state = State(tmp_path / 'history.sqlite3')
    old = Article('脑机星链发布家用助眠仪', 'https://news.example/old',
                  '2026-10-09', '报道', summary='脑机星链推出家用助眠仪。',
                  companies=['脑机星链'], category='行业')
    new = Article('脑机星链携助眠仪亮相展会', 'https://other.example/new',
                  '2026-10-12', '报道', summary='脑机星链展示家用助眠仪。',
                  companies=['脑机星链'], category='行业')
    state.record_sent_events([old], date(2026, 10, 9))
    class AI:
        calls = 0
        def chat(self, *args, **kwargs):
            self.calls += 1
            return {'duplicate': True}
    ai = AI()
    history = state.recent_sent_events(DAY, 60)
    assert previously_covered(new, history, ai, state, DAY)
    assert previously_covered(new, history, ai, state, DAY)
    assert ai.calls == 1
    state.close()


def test_new_publisher_is_scored_once_and_exported(tmp_path):
    state = State(tmp_path / 'history.sqlite3')
    item = Article('脑电报道', 'https://hn.china.com/gundong/one', str(DAY), '中华网')
    scoring = {'source_weights': {'authority': .7, 'recognition': .3}, 'source_domains': {}}
    attach(item, state, scoring, DAY)
    assert (item.source_authority, item.source_recognition) == (45, 60)
    assert source_factor(item, scoring) == .495
    attach(item, state, scoring, DAY)
    assert item.source_authority == 45  # No repeat calculation for the same source.
    attach(item, state, {'source_domains': {'china.com': [99, 99]}}, DAY)
    assert item.source_authority == 99  # Explicit editorial overrides remain possible.
    out = tmp_path / 'sources.csv'
    state.export_source_authority(out)
    assert '中华网' in out.read_text(encoding='utf-8-sig')
    state.close()


def test_finalist_reads_page_and_finds_old_event_without_search(tmp_path):
    state = State(tmp_path / 'history.sqlite3')
    item = Article('脑机星链亮相展会', 'https://news.example/item', str(DAY),
                   '媒体', summary='搜索摘要称脑机星链展示助眠产品。', date_evidence='search')
    class Reader:
        def get(self, url):
            assert url == item.url
            return '<meta property="article:published_time" content="2026-10-12"><article>9月21日，脑机星链在深圳展会展示家用助眠仪。</article>'
    class AI:
        def chat(self, *args, **kwargs):
            return {'relevant': True, 'single_event': True, 'event_date': '2026-09-21',
                    'date_quote': '9月21日', 'focus_quote': '', 'focus_title': ''}
    assert verify_finalist(item, Reader(), AI(), state, DAY)
    assert item.event_date == '2026-09-21'
    assert not item.in_window(DAY, {'行业': 3}, True)
    assert not item.source_summary  # Search prose is not treated as read article text.
    state.close()


def test_roundup_needs_a_dated_focused_event(tmp_path):
    state = State(tmp_path / 'history.sqlite3')
    item = Article('人工智能行业（10.05-10.11）', 'https://news.example/weekly', str(DAY),
                   '周报', date_evidence='search')
    class Reader:
        def get(self, url):
            return '<meta property="article:published_time" content="2026-10-12"><article>10月10日前沿类脑完成天使轮融资。10月11日其他公司发布新产品。</article>'
    class AI:
        def chat(self, *args, **kwargs):
            return {'relevant': True, 'single_event': True, 'event_date': '2026-10-10',
                    'date_quote': '10月10日', 'focus_quote': '', 'focus_title': ''}
    assert not verify_finalist(item, Reader(), AI(), state, DAY)
    state.close()


def test_roundup_can_be_focused_on_its_actual_dated_event(tmp_path):
    state = State(tmp_path / 'history.sqlite3')
    item = Article('人工智能行业（10.05-10.11）', 'https://news.example/weekly', str(DAY),
                   '周报', date_evidence='search')
    sentence = '10月10日前沿类脑完成2.5亿元天使轮融资。'
    class Reader:
        def get(self, url):
            return '<meta property="article:published_time" content="2026-10-12"><article>' + sentence + '10月11日其他公司发布新产品。</article>'
    class AI:
        def chat(self, *args, **kwargs):
            return {'relevant': True, 'single_event': True, 'event_date': '2026-10-10',
                    'date_quote': '10月10日', 'focus_quote': sentence,
                    'focus_title': '前沿类脑完成2.5亿元天使轮融资'}
    assert verify_finalist(item, Reader(), AI(), state, DAY)
    assert item.event_date == '2026-10-10'
    assert item.display_title == '前沿类脑完成2.5亿元天使轮融资'
    assert item.summary == sentence
    state.close()


def test_unreadable_wechat_search_hit_is_not_passed_as_full_text(tmp_path):
    state = State(tmp_path / 'history.sqlite3')
    item = Article('微信报道', 'https://mp.weixin.qq.com/s/example', str(DAY),
                   '公众号', summary='搜索摘要', date_evidence='search')
    class Reader:
        def get(self, url):
            raise ValueError('robots禁止抓取')
    assert not verify_finalist(item, Reader(), object(), state, DAY)
    state.close()


def test_recent_page_without_event_date_keeps_its_publication_date(tmp_path):
    state = State(tmp_path / 'history.sqlite3')
    item = Article('天津大学获批脑机接口博士点', 'https://news.example/reprint',
                   str(DAY), '媒体', summary='搜索摘要称近日获批。', date_evidence='search')
    class Reader:
        def get(self, url):
            return '<meta property="article:published_time" content="2026-10-12"><article>天津大学已获批脑机接口博士点。</article>'
    class AI:
        def chat(self, *args, **kwargs):
            return {'relevant': True, 'single_event': True, 'event_date': '', 'date_quote': '',
                    'focus_quote': '', 'focus_title': ''}
    assert verify_finalist(item, Reader(), AI(), state, DAY)
    assert item.content_verified
    assert item.published == '2026-10-12'
    assert item.event_date == ''
    assert item.in_window(DAY, {'行业': 3}, True)
    state.close()


def test_page_without_any_date_keeps_existing_discovery_date(tmp_path):
    state = State(tmp_path / 'history.sqlite3')
    item = Article('企业发布脑电耳机', 'https://news.example/item', str(DAY),
                   '媒体', summary='搜索摘要', date_evidence='search')
    class Reader:
        def get(self, url):
            return '<article>企业发布脑电耳机，用于睡眠监测。</article>'
    class AI:
        def chat(self, task, payload, **kwargs):
            assert payload['publication_date'] == ''
            return {'relevant': True, 'single_event': True, 'event_date': '',
                    'date_quote': '', 'focus_quote': '', 'focus_title': ''}
    assert verify_finalist(item, Reader(), AI(), state, DAY)
    assert item.published == str(DAY)
    assert item.date_evidence == 'search'
    assert item.event_date == ''
    state.close()


def test_clear_review_day_preserves_raw_candidates_and_sent_records(tmp_path):
    state = State(tmp_path / 'history.sqlite3')
    unsent = Article('未发送脑电耳机报道', 'https://news.example/unsent', str(DAY), '媒体')
    sent = Article('已发送脑电耳机报道', 'https://news.example/sent', str(DAY), '媒体')
    untouched = Article('未分析的候选', 'https://news.example/raw', str(DAY), '媒体')
    for item in (unsent, sent, untouched):
        state.retain_candidates([item], DAY)
        if item is not untouched:
            item.relevance, item.accepted = 90, True
        state.save(item)
    state.mark([sent], DAY, 'sent')
    state.record_sent_events([sent], DAY)
    for item in (unsent, sent):
        state.put('ai:' + item.identity, {'day': str(DAY), 'row': {'relevance': 90}})
    state.put('event-v1:one', {'day': str(DAY), 'result': {}})
    result = state.clear_unsent_reviews(DAY)
    assert result['reviewed_removed'] == 1
    assert state.cached(unsent) is None
    assert state.get('ai:' + unsent.identity) is None
    assert state.get('event-v1:one') is None
    assert state.cached(sent) is not None
    assert state.get('ai:' + sent.identity) is not None
    assert state.sent(sent)
    assert state.cached(untouched) is not None
    assert {item.identity for item in state.candidates()} == {
        unsent.identity, sent.identity, untouched.identity}
    state.close()


def test_search_date_is_replaced_by_read_page_date(tmp_path):
    state = State(tmp_path / 'history.sqlite3')
    item = Article('脑电耳机发布', 'https://news.example/reprint', str(DAY),
                   '媒体', summary='搜索摘要', date_evidence='search')
    class Reader:
        def get(self, url):
            return '<meta property="article:published_time" content="2026-09-19"><article>9月19日，企业发布脑电耳机。</article>'
    class AI:
        def chat(self, *args, **kwargs):
            return {'relevant': True, 'single_event': True, 'event_date': '2026-09-19',
                    'date_quote': '9月19日', 'focus_quote': '', 'focus_title': ''}
    assert verify_finalist(item, Reader(), AI(), state, DAY)
    assert item.published == '2026-09-19'
    assert not item.in_window(DAY, {'行业': 3}, True)
    state.close()


def test_event_date_from_page_without_publication_header(tmp_path):
    state = State(tmp_path / 'history.sqlite3')
    item = Article('企业推出脑电耳机', 'https://news.example/undated', str(DAY),
                   '媒体', date_evidence='search')
    class Reader:
        def get(self, url):
            return '<article>10月12日，企业推出脑电耳机。</article>'
    class AI:
        def chat(self, *args, **kwargs):
            return {'relevant': True, 'single_event': True, 'event_date': '2026-10-12',
                    'date_quote': '10月12日', 'focus_quote': '10月12日，企业推出脑电耳机。',
                    'focus_title': ''}
    assert verify_finalist(item, Reader(), AI(), state, DAY)
    assert item.published == str(DAY)
    assert item.in_window(DAY, {'行业': 3}, True)
    assert '2026-10-12' in render([item], DAY)[1]
    state.close()


def test_full_page_can_overrule_misleading_search_relevance(tmp_path):
    state = State(tmp_path / 'history.sqlite3')
    item = Article('智能教育产品', 'https://news.example/education', str(DAY),
                   '媒体', summary='搜索摘要错误地说这是一款脑电耳机。', date_evidence='search')
    class Reader:
        def get(self, url):
            return ('<meta property="article:published_time" content="2026-10-12">'
                    '<article>10月12日，企业发布普通学习平板，不涉及脑电或耳机。</article>')
    class AI:
        def chat(self, *args, **kwargs):
            return {'relevant': False, 'single_event': True, 'event_date': '2026-10-12',
                    'date_quote': '10月12日', 'focus_quote': '', 'focus_title': ''}
    assert not verify_finalist(item, Reader(), AI(), state, DAY)
    state.close()


def test_already_read_undated_page_reuses_body_not_discovery_as_publication(tmp_path):
    state = State(tmp_path / 'history.sqlite3')
    item = Article('脑电耳机发布', 'https://trusted.example/one', str(DAY),
                   '定向站', date_evidence='recent_first_seen',
                   first_reported=str(DAY), body='10月12日，企业发布脑电耳机。')
    class Reader:
        def get(self, url):
            raise AssertionError('already-read original page must not be fetched twice')
    class AI:
        def chat(self, task, payload, **kwargs):
            assert payload['publication_date'] == ''
            return {'relevant': True, 'single_event': True, 'event_date': '2026-10-12',
                    'date_quote': '10月12日', 'focus_quote': '', 'focus_title': ''}
    assert verify_finalist(item, Reader(), AI(), state, DAY)
    assert item.event_date == '2026-10-12'
    state.close()


def test_page_check_reuses_ai_result_and_corrects_focus(tmp_path):
    state = State(tmp_path / 'history.sqlite3')
    sentence = '10月12日，华东师大展示超声脑机接口。'
    class Reader:
        def get(self, url):
            return ('<meta property="article:published_time" content="2026-10-12">'
                    '<article>激光器与原子钟。' + sentence + '</article>')
    class AI:
        calls = 0
        def chat(self, *args, **kwargs):
            self.calls += 1
            return {'relevant': True, 'single_event': True, 'event_date': '2026-10-12',
                    'date_quote': '10月12日', 'focus_quote': sentence,
                    'focus_title': '华东师大展示超声脑机接口', 'subject': '华东师大',
                    'category': '行业', 'tags': ['超声脑机接口']}
    ai = AI()
    for _ in range(2):
        item = Article('华东师大成果亮相', 'https://news.example/ecnu', str(DAY),
                       '媒体', summary='搜索摘要仅提到激光器。', date_evidence='search')
        assert verify_finalist(item, Reader(), ai, state, DAY)
        assert item.summary == sentence
        assert item.subject == '华东师大'
        assert item.tags == ['超声脑机接口']
    assert ai.calls == 1
    state.close()


def test_existing_alternate_source_is_read_without_another_search(tmp_path):
    state = State(tmp_path / 'history.sqlite3')
    item = Article('前沿类脑融资', 'https://mp.weixin.qq.com/s/example', str(DAY),
                   '公众号', date_evidence='search',
                   sources=[{'name': '公众号', 'url': 'https://mp.weixin.qq.com/s/example'},
                            {'name': '投资界', 'url': 'https://news.pedaily.cn/story'}])
    class Reader:
        calls = []
        def get(self, url):
            self.calls.append(url)
            if 'weixin' in url:
                raise ValueError('robots禁止抓取')
            return '<meta property="article:published_time" content="2026-10-12"><article>10月12日前沿类脑完成天使轮融资。</article>'
    class AI:
        def chat(self, *args, **kwargs):
            return {'relevant': True, 'single_event': True, 'event_date': '2026-10-12',
                    'date_quote': '10月12日', 'focus_quote': '', 'focus_title': ''}
    reader = Reader()
    assert verify_finalist(item, reader, AI(), state, DAY)
    assert reader.calls == ['https://mp.weixin.qq.com/s/example', 'https://news.pedaily.cn/story']
    assert item.url == 'https://news.pedaily.cn/story'
    assert item.source == '投资界'
    state.close()


def test_sent_events_outlive_three_day_candidate_window(tmp_path):
    state = State(tmp_path / 'history.sqlite3')
    old = Article('脑机星链发布助眠仪', 'https://news.example/old',
                  str(DAY), '媒体', category='行业')
    state.save(old)
    state.mark([old], DAY, 'sent')
    state.record_sent_events([old], DAY)
    state.prune(DAY + timedelta(days=10), 3, drop_undated=True)
    assert len(state.recent_sent_events(DAY + timedelta(days=10), 60)) == 1
    preview = state.preview_without_delivery_history()
    assert preview.recent_sent_events(DAY + timedelta(days=10), 60) == []
    preview.close()
    state.close()


def test_pipeline_refills_after_full_page_rejects_old_event(tmp_path, monkeypatch):
    cfg, policy, _ = pipeline.load(__import__('pathlib').Path(__file__).resolve().parents[1])
    old = Article('脑机星链亮相展会', 'https://news.example/old', str(DAY), '媒体',
                  summary='脑机星链在展会上展示助眠设备。', date_evidence='search')
    fresh = Article('前沿类脑发布脑电耳机', 'https://news.example/fresh', str(DAY), '媒体',
                    summary='前沿类脑发布新款脑电耳机。', date_evidence='search')
    class Reader:
        def get(self, url):
            day, body = (('9月21日', '9月21日脑机星链在展会上展示助眠设备。')
                         if url.endswith('/old') else
                         ('10月12日', '10月12日前沿类脑发布新款脑电耳机。'))
            return '<meta property="article:published_time" content="2026-10-12"><article>' + body + '</article>'
    class AI:
        def chat(self, task, payload, **kwargs):
            day = '9月21日' if payload['title'].endswith('展会') else '10月12日'
            return {'relevant': True, 'single_event': True, 'event_date': '2026-09-21' if day.startswith('9') else '2026-10-12',
                    'date_quote': day, 'focus_quote': '', 'focus_title': ''}
    def assess(rows, *args):
        for row in rows:
            row.accepted, row.relevance = True, 90
        return rows
    monkeypatch.setattr(pipeline, 'relevance_filter', assess)
    monkeypatch.setattr(pipeline, 'merge_event_reports', lambda rows, *args: rows)
    monkeypatch.setattr(pipeline, 'finish_item', lambda *args, **kwargs: True)
    state = State(tmp_path / 'pipeline.sqlite3')
    _, picks = pipeline.process_candidates([old, fresh], cfg, policy['rules'], policy['exclude'],
                                            state, AI(), Reader(), DAY, lambda *args, **kwargs: None)
    assert [a.title for a in picks] == [fresh.title]
    assert picks[0].content_verified
    state.close()
