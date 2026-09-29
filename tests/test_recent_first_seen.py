from datetime import date
from pathlib import Path

import pytest

from brainsong import pipeline
from brainsong.editor import apply_assessment, fill_link_only, render, select, summarize
from brainsong.model import Article
from brainsong.progress import Progress
from brainsong.provider import assess
from brainsong.state import State


ROOT = Path(__file__).resolve().parents[1]
DAY = date(2026, 9, 29)


class Reader:
    def __init__(self, html):
        self.html = html
        self.calls = 0

    def get(self, url):
        self.calls += 1
        return self.html


@pytest.mark.parametrize('html,expected_label', [
    ('<article><p>一家企业发布脑电耳机，支持睡眠分析。</p></article>', '近期'),
    ('<meta property="article:published_time" content="2026-09-28"><article>脑电耳机</article>', '2026-09-28'),
])
def test_first_seen_fallback_only_after_reading_original(tmp_path, monkeypatch, html, expected_label):
    cfg, policy, _ = pipeline.load(ROOT)
    item = Article('脑电耳机新品融资', 'https://trusted.example/news/1', '', '定向站',
                   '一家企业发布脑电耳机，支持睡眠分析。', date_evidence='no_date_hint')
    state = State(tmp_path / 'state.sqlite3')
    reader = Reader(html)

    def assess(batch, *args):
        for article in batch:
            article.accepted = True
            article.relevance = 90
        return batch

    monkeypatch.setattr(pipeline, 'relevance_filter', assess)
    monkeypatch.setattr(pipeline, 'merge_event_reports', lambda items, *args: items)
    monkeypatch.setattr(pipeline, 'finish_item', lambda *args, **kwargs: True)
    _, picks = pipeline.process_candidates([item], cfg, policy['rules'], policy['exclude'],
        state, object(), reader, DAY, Progress(tmp_path / 'progress.log'),
        recent_undated_ids={item.identity})
    assert reader.calls == 1
    assert len(picks) == 1
    assert picks[0].date_evidence == ('recent_first_seen' if expected_label == '近期' else 'article_meta')
    assert expected_label in render(picks, DAY)[1]
    state.close()


def test_old_or_previously_seen_undated_item_does_not_become_recent(tmp_path):
    state = State(tmp_path / 'state.sqlite3')
    item = Article('脑电耳机融资', 'https://trusted.example/news/2', '', '定向站',
                   date_evidence='no_date_hint')
    assert not state.ever_seen(item)
    state.retain_candidates([item], DAY)
    assert state.ever_seen(item)
    item.published = '2026-09-29'
    item.first_reported = '2026-09-29'
    item.date_evidence = 'recent_first_seen'
    state.retain_candidates([item], DAY)
    new_listing = Article(item.title, item.url, '', item.source, date_evidence='no_date_hint')
    state.retain_candidates([new_listing], date(2026, 9, 30))
    restored = state.candidates()[0]
    assert restored.published == '2026-09-29'
    assert restored.first_reported == '2026-09-29'
    assert restored.date_evidence == 'recent_first_seen'
    state.close()


def test_undated_global_search_and_policy_sources_not_opted_in():
    _, _, sources = pipeline.load(ROOT)
    allowed = {source['id'] for source in sources if source.get('allow_recent_without_date')}
    assert {'zhidx', 'neurable', 'emotiv', 'stcn', 'bciwiki-funding'} <= allowed
    assert not any(source['kind'] == 'search' or source['category'] == '政策'
                   or source['kind'] == 'arxiv' for source in sources
                   if source.get('allow_recent_without_date'))


def test_ai_receives_discovery_date_separately_from_publication_date(tmp_path):
    item = Article('脑电耳机新品', 'https://trusted.example/news/3', DAY.isoformat(), '定向站',
                   '一家企业发布脑电耳机。', first_reported=DAY.isoformat(),
                   date_evidence='recent_first_seen')

    class AI:
        def chat(self, task, payload):
            row = payload['articles'][0]
            assert row['date'] == ''
            assert row['first_seen'] == DAY.isoformat()
            assert '不是发表或事件日期' in task
            return {'items': [{'id': row['id'], 'relevance': 90, 'accept': True,
                               'category': '行业', 'tags': []}]}

    assert assess(AI(), [item], '消费级脑电', [])[item.identity]['accept']

    item.summary = ''
    item.body = '一家企业发布脑电耳机，支持睡眠分析和脑电数据采集。'
    item.capture_source()
    state = State(tmp_path / 'state.sqlite3')

    class SummaryAI:
        def chat(self, task, payload):
            assert payload['publication_date'] == ''
            assert payload['first_seen'] == DAY.isoformat()
            assert '不能写成当天发布' in task
            return {'summary': '一家企业发布脑电耳机，支持睡眠分析和脑电数据采集。'}

    summarize(item, SummaryAI(), state, DAY)
    assert item.summary_kind == 'ai'
    state.close()


@pytest.mark.parametrize('category,allowed', [('行业', True), ('资本', True),
                                               ('政策', False), ('学术', False)])
def test_ai_reclassified_undated_item_cannot_use_recent_for_policy_or_academic(tmp_path, category, allowed):
    cfg, _, _ = pipeline.load(ROOT)
    item = Article('脑机接口新消息', f'https://trusted.example/news/{category}', DAY.isoformat(),
                   '定向站', '北京企业发布脑电产品并介绍最新进展。',
                   first_reported=DAY.isoformat(), date_evidence='recent_first_seen')
    row = {'relevance': 90, 'accept': True, 'category': category,
           'tags': ['脑电'], 'source_kind': 'original'}
    apply_assessment(item, row, cfg['scoring'], cfg['policy_scope'])
    assert item.accepted is allowed
    state = State(tmp_path / 'state.sqlite3')
    assert bool(select([item], state, DAY, cfg)) is allowed
    item.accepted = True  # Even stale cached acceptance must not bypass final selection.
    item.summary_kind = 'failed'
    assert bool(fill_link_only([], [item], state, DAY, cfg)) is allowed
    state.close()
