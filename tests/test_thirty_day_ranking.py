from datetime import date, timedelta
from pathlib import Path

import pytest

from brainsong.collect import from_search
from brainsong.editor import relevance_filter, select, prepare
from brainsong.model import Article, freshness_factor, ranking_score, shortlist_score, dictionary_result
from brainsong.pipeline import load, queries
from brainsong.provider import QualityError
from brainsong.state import State

DAY = date(2026, 9, 17)
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('age,factor', [(-1, 0), (0, 1), (1, .8), (2, .8), (3, .45), (6, .45), (7, .2), (13, .2), (14, .05), (29, .05), (30, 0)])
def test_boundaries(age, factor):
    item = Article('脑电', 'https://example.org/a', str(DAY-timedelta(days=age)), '测试', score=90, source_kind='original')
    assert freshness_factor(item, DAY) == factor
    assert shortlist_score(item, DAY) == round(90*factor, 2)
    assert ranking_score(item, DAY) == 69.75
    assert item.score == 90


def test_dictionary_banned_not_bciwiki():
    assert dictionary_result('https://www.merriam-webster.com/dictionary/neural')
    assert not dictionary_result('https://bciwiki.com/item/a')
    assert not dictionary_result('https://dictionary.com.example.org/a')
    rows = [{'title':'Neural meaning','link':'https://dictionary.com/neural'}]
    assert from_search(rows) == []
    assert not prepare(Article('脑电定义', rows[0]['link'], str(DAY), '词典'), [], [])


def test_active_profile_limits_all_categories_to_three_days(tmp_path):
    cfg, _, _ = load(ROOT)
    assert set(cfg['windows'].values()) == {3}
    state = State(tmp_path/'test.db')
    fresh = Article('新品', 'https://example.org/a', str(DAY), '测试', score=70)
    old = Article('标准', 'https://example.org/b', str(DAY-timedelta(days=15)), '测试', category='政策', score=100)
    assert shortlist_score(old, DAY) == 5
    assert select([old, fresh], state, DAY, cfg) == [fresh]
    state.close()


def test_length_failure_retries_smaller_inputs_and_batches(tmp_path):
    cfg, policy, _ = load(ROOT)
    rows = [Article(f'脑电{i}', f'https://example.org/{i}', str(DAY), '测试', summary='公开摘要内容。'*120) for i in range(4)]
    class AI:
        calls = []
        def chat(self, task, payload):
            articles = payload['articles']
            self.calls.append((len(articles), len(articles[0]['evidence'])))
            if len(self.calls) == 1:
                raise QualityError('chat_length')
            return {'items': [dict(id=a['id'], relevance=85, accept=True, category='行业', tags=['脑电']) for a in articles]}
    ai = AI()
    state = State(tmp_path/'test.db')
    assert len(relevance_filter(rows, ai, cfg, policy['rules'], state, DAY)) == 4
    assert ai.calls == [(3, 700), (1, 350), (1, 350), (1, 350), (1, 700)]
    state.close()


def test_queries_are_short_without_synonym_bundles():
    cfg, policy, _ = load(ROOT)
    assert all(' OR ' not in q and len(q) <= 55 for _, q in queries(policy['rules'], cfg, DAY))


def test_semantic_reprint_inherits_sent_alias(tmp_path):
    from brainsong.editor import merge_event_reports
    state = State(tmp_path/'test.db')
    old = Article('某脑电企业新品发布', 'https://example.org/old', str(DAY), '官网', score=70)
    new = Article('该耳机产品上市报道', 'https://example.org/new', str(DAY), '媒体', score=90)
    state.mark([old], DAY, 'sent')
    class AI:
        def chat(self, *args):
            return {'groups': [['A0', 'A1']]}
    merged = merge_event_reports([old, new], AI(), state, DAY)
    assert len(merged) == 1 and state.sent(merged[0])
    state.close()


def test_different_funding_rounds_never_merge(tmp_path):
    from brainsong.editor import merge_event_reports
    state = State(tmp_path/'test.db')
    rows = [Article(f'同一企业完成{r}轮融资', f'https://example.org/{r}', str(DAY), '媒体', category='资本') for r in ['C', 'D']]
    class AI:
        def chat(self, *args):
            return {'groups': [['A0', 'A1']]}
    assert len(merge_event_reports(rows, AI(), state, DAY)) == 2
    state.close()
