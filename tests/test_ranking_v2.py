from datetime import date, timedelta
from types import SimpleNamespace

from brainsong.editor import select, apply_assessment, merge
from brainsong.model import Article, ranking_score, shortlist_score
from brainsong.pipeline import load
from brainsong.state import State
from test_brainsong import ROOT

DAY = date(2026, 9, 18)


def test_company_aliases_and_category_caps(tmp_path):
    cfg, _, _, _ = load(ROOT)
    state = State(tmp_path / 'state.db')
    rows = [Article(f'{name}发布脑电耳机{i}', f'https://example.org/{i}', str(DAY), '官网',
                    score=90, category=['行业','资本','学术'][i%3])
            for i, name in enumerate(['强脑科技','BrainCo','强脑','甲公司','乙公司','丙公司','丁公司','戊公司','己公司'])]
    picks = select(rows, state, DAY, cfg)
    assert len(picks) == 7
    assert sum(a in rows[:3] for a in picks) <= 2
    assert all(sum(a.category == c for a in picks) <= 3 for c in ['行业','资本','学术'])
    state.close()


def test_event_bonus_requires_quote_and_does_not_stack():
    a = Article('公司发布脑电耳机正式新品', 'https://example.org/a', str(DAY), '官网')
    row = dict(relevance=90, accept=True, category='行业', tags=[], event_type='product', source_kind='original')
    apply_assessment(a, row)
    assert ranking_score(a, DAY) == 69.75
    apply_assessment(a, dict(row, event_evidence='公司发布脑电耳机正式新品'))
    assert ranking_score(a, DAY) == 69.75  # Event bonuses no longer enter either score.
    assert ranking_score(a, DAY+timedelta(days=7)) == 69.75
    assert shortlist_score(a, DAY+timedelta(days=7)) == 18


def test_reprint_keeps_old_event_date():
    a = Article('相同新闻', 'https://example.org/a', str(DAY-timedelta(days=15)), '媒体', score=90)
    b = Article('相同新闻', 'https://example.org/b', str(DAY), '媒体', score=90)
    merged = merge([b, a])[0]
    assert shortlist_score(merged, DAY) == 4.5


def test_partial_brief_can_send(tmp_path, monkeypatch):
    from brainsong import pipeline
    cfg, policy, _, _ = load(ROOT)
    cfg['search_enabled'] = False
    a = Article('脑电耳机新品', 'https://example.org/a', str(DAY), '官网', summary='脑电耳机支持课堂中的学习状态监测。')
    monkeypatch.setattr(pipeline, 'load', lambda root: (cfg, policy, [], []))
    monkeypatch.setattr(pipeline, 'api_key', lambda: 'offline-test-key')
    monkeypatch.setattr(pipeline, 'process_candidates', lambda *args, **kwargs: ([a], [a]))
    calls = []
    class Pusher:
        def __init__(self, webhook):
            pass
        def send(self, title, body):
            calls.append(body)
            return SimpleNamespace(success=True)
    monkeypatch.setattr(pipeline, 'FeishuPusher', Pusher)
    monkeypatch.setenv('FEISHU_WEBHOOK', 'https://open.feishu.cn/open-apis/bot/v2/hook/test-only')
    monkeypatch.setenv('DAILY_DELIVERY', 'true')
    result = pipeline.run(tmp_path, DAY)
    assert not result['sent']
    result = pipeline.run(tmp_path, DAY, send=True)
    assert result['sent'] and result['items'] == 1 and len(calls) == 1
    state = State(tmp_path/'state/brainsong.sqlite3')
    assert state.get('daily-delivery:' + str(DAY)) == 'sent'
    state.close()
