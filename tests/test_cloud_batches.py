from datetime import date
from pathlib import Path

import pytest

from brainsong import pipeline
from brainsong.model import Article
from brainsong.progress import Progress
from brainsong.state import State

DAY = date(2026, 9, 18)
ROOT = Path(__file__).resolve().parents[1]


class NoNetwork:
    def get(self, url):
        pytest.fail('Unexpected page request: ' + url)


def make_item(n, category='行业', published='2026-09-18', title=None):
    category = ['行业', '资本', '政策'][n % 3] if category == '行业' else category
    if category == '资本' and title is None:
        title = f'脑机接口新品融资进展{n}'
    return Article(title or f'脑机接口新品研发进展{n}', f'https://example.org/{n}',
                   published, '官网', '公司发布脑电采集设备，提供面向教育和睡眠研究的数据采集功能。', category=category)


def run_batch(tmp_path, monkeypatch, items, reject_first=False, counts=None, cfg_updates=None):
    cfg, policy, _, _ = pipeline.load(ROOT)
    cfg.update(cfg_updates or {})
    state = State(tmp_path / 'test.sqlite3')
    if counts:
        state.put('counts', counts)
    state.retain_candidates(items, DAY)
    assessed = []
    def assess(batch, *args):
        assessed.extend(a.identity for a in batch)
        for a in batch:
            a.accepted = bool(a.matches)
        return [a for a in batch if a.accepted]
    monkeypatch.setattr(pipeline, 'relevance_filter', assess)
    monkeypatch.setattr(pipeline, 'merge_event_reports', lambda items, *args: items)
    def finish(item, *args, **kwargs):
        return not (reject_first and item.url.endswith('/0'))
    monkeypatch.setattr(pipeline, 'finish_item', finish)
    pool, picks = pipeline.process_candidates(items, cfg, policy['rules'], policy['exclude'],
        state, object(), NoNetwork(), DAY, Progress(tmp_path / 'progress.log'))
    return state, assessed, pool, picks


def test_weighted_batches_stop_and_keep_remaining(tmp_path, monkeypatch):
    unrelated = [make_item(100+i, '政策', title=f'普通财政事项公示{i}') for i in range(100)]
    relevant = [make_item(i) for i in range(30)]
    state, assessed, pool, picks = run_batch(tmp_path, monkeypatch, unrelated + relevant)
    assert len(picks) == 7
    assert 12 <= len(assessed) <= 24  # Category quotas may require a third batch, then one check.
    assert all(a.matches for a in picks)
    assert len(state.candidates()) == len(pool) == 130
    assert state.db.execute('select count(*) from delivered').fetchone()[0] == 0
    state.close()


def test_many_unknown_dates_cannot_starve_ready_news(tmp_path, monkeypatch):
    missing = [make_item(100+i, published='') for i in range(60)]
    ready = [make_item(i) for i in range(9)]
    state, assessed, _, picks = run_batch(tmp_path, monkeypatch, missing + ready,
                                          cfg_updates={'max_analysis_candidates': 9})
    assert len(picks) == 7
    assert set(assessed).issubset({a.identity for a in ready})
    assert len(state.candidates()) == 69
    state.close()


def test_full_academic_direction_yields_to_industry_and_capital():
    picks = [make_item(i, '学术') for i in range(3)]
    more_papers = [make_item(i+10, '学术') for i in range(20)]
    industry = make_item(30)
    capital = make_item(31)
    estimate = lambda a: 100 if a.category == '学术' else 70
    batch = pipeline.direction_batch(more_papers + [industry, capital], picks,
                                     {'max_category_items': 3}, estimate, 6)
    assert batch == [industry, capital] or batch == [capital, industry]


def test_quality_precedes_forced_direction_round_robin():
    papers = [make_item(i, '学术') for i in range(20)]
    other = [make_item(30), make_item(31), make_item(32)]
    batch = pipeline.direction_batch(papers+other, [], {}, lambda a: 100 if a.category == '学术' else 70, 6)
    assert all(a.category == '学术' for a in batch)


def test_skipped_metadata_does_not_spend_ai_budget(tmp_path, monkeypatch):
    cfg, policy, _, _ = pipeline.load(ROOT)
    cfg['max_analysis_candidates'] = 3
    state = State(tmp_path / 'state.db')
    rows = [make_item(i) for i in range(9)]
    for a in rows:
        a.summary = a.source_summary = a.body = a.source_excerpt = ''
    reads = []
    class Reader:
        def get(self, url):
            reads.append(url)
            return 'old' if len(reads) <= 6 else 'new'
    def metadata(a, html):
        a.published = '2020-01-01' if html == 'old' else str(DAY)
        a.summary = '公司发布脑电耳机新品，支持脑电数据采集和教育研究。'
    assessed = []
    def assess(batch, *args):
        assessed.extend(batch)
        return batch
    monkeypatch.setattr(pipeline, 'metadata', metadata)
    monkeypatch.setattr(pipeline, 'relevance_filter', assess)
    monkeypatch.setattr(pipeline, 'merge_event_reports', lambda items,*args: items)
    monkeypatch.setattr(pipeline, 'finish_item', lambda *args,**kwargs: True)
    pipeline.process_candidates(rows,cfg,policy['rules'],policy['exclude'],state,object(),Reader(),DAY,lambda *args,**kwargs: None)
    assert len(reads) == 9 and len(assessed) == 3
    state.close()


def test_expired_articles_never_fetch_or_score(tmp_path, monkeypatch):
    old = make_item(90, published='2025-01-01')
    old.summary = old.body = old.source_summary = old.source_excerpt = ''
    state, assessed, _, picks = run_batch(tmp_path, monkeypatch, [old] + [make_item(i) for i in range(12)])
    assert old.identity not in assessed
    assert len(picks) == 7
    assert old.identity in {a.identity for a in state.candidates()}
    state.close()


def test_failed_summary_refills_instead_of_stopping(tmp_path, monkeypatch):
    state, _, _, picks = run_batch(tmp_path, monkeypatch, [make_item(i) for i in range(15)], True)
    assert len(picks) == 7
    assert all(not a.url.endswith('/0') for a in picks)
    state.close()


def test_academic_cap_survives_batches(tmp_path, monkeypatch):
    items = [make_item(i, '学术') for i in range(12)] + [make_item(i+30) for i in range(15)]
    state, _, _, picks = run_batch(tmp_path, monkeypatch, items)
    assert len(picks) == 7
    assert sum(a.category == '学术' for a in picks) <= 3
    state.close()


def test_errors_are_flushed_before_completion(tmp_path):
    state = State(tmp_path / 'test.sqlite3')
    path = tmp_path / 'progress.log'
    state.progress = Progress(path)
    state.error(DAY, '文章读取:https://example.org/private-query', ValueError('secret-response'))
    text = path.read_text(encoding='utf-8')
    assert 'ValueError' in text
    assert 'secret-response' not in text and 'private-query' not in text
    state.close()


def test_due_exploration_gets_a_chance_before_stop(tmp_path, monkeypatch):
    discovery = make_item(100, title='柔性生物传感初创发布技术')
    discovery.summary = '初创团队发布柔性生物传感设备，用于消费级可穿戴产品。'
    state, assessed, _, picks = run_batch(tmp_path, monkeypatch,
        [make_item(i) for i in range(30)] + [discovery], counts={'total': 7, 'exploration': 0})
    assert discovery.identity in assessed[:6]
    assert len(picks) == 7
    # Exploration gets analysis opportunity, not a guaranteed final slot.
    assert sum(a.exploration for a in picks) <= 1
    state.close()
