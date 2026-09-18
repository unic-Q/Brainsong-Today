from datetime import date

from brainsong.editor import compact_title, render, select
from brainsong.model import Article
from brainsong.state import State


DAY = date(2026, 9, 17)


def test_title_and_summary_independent(tmp_path):
    a = Article('脑电研究' * 50, 'https://example.org/a', str(DAY), '测试', summary='脑电系统用于学习状态监测。')
    original = a.title
    class AI:
        def chat(self, task, payload):
            assert '100字限制仅用于摘要' in task
            return {'title': '脑电研究中的数据采集与跨场景验证进展'}
    state = State(tmp_path / 'a.db')
    compact_title(a, AI(), state, DAY)
    assert a.title == original
    assert a.display_title != original
    assert a.summary == '脑电系统用于学习状态监测。'
    _, body = render([a], [], DAY)
    assert a.display_title in body and a.summary in body
    state.close()


def test_no_title_hard_cut():
    title = '脑电研究' * 60
    a = Article(title, 'https://example.org/a', str(DAY), '测试')
    assert title in render([a], [], DAY)[1]


def test_select_and_render_seven(tmp_path):
    state = State(tmp_path / 'a.db')
    rows = [Article(f'脑电产品更新{i}', f'https://example.org/{i}', str(DAY), '测试', score=80, category=['行业','资本','政策'][i%3]) for i in range(9)]
    cfg = {'max_items': 7, 'windows': {'行业': 30, '资本':30, '政策':30}, 'exploration_ratio': .1}
    picks = select(rows, state, DAY, cfg)
    assert len(picks) == 7
    body = render(rows, [], DAY)[1]
    assert '**7.' in body and '**8.' not in body
    state.close()


def test_pipeline_replenishes_failed_summaries(tmp_path, monkeypatch):
    import brainsong.pipeline as pipeline
    root = __import__('pathlib').Path(__file__).resolve().parents[1]
    cfg, policy, _, _ = pipeline.load(root)
    cfg['search_enabled'] = False
    rows = [Article(f'脑电耳机{"融资" if i%3 == 1 else "新品"}{i}', f'https://example.org/{i}', str(DAY), '测试',
                    summary='脑电耳机支持课堂中的学习状态监测。', category=['行业','资本','政策'][i%3]) for i in range(12)]
    monkeypatch.setattr(pipeline, 'load', lambda root: (cfg, policy, [{'id': 'test', 'kind': 'html'}], []))
    monkeypatch.setattr(pipeline, 'api_key', lambda: 'test-no-network')
    monkeypatch.setattr(pipeline, 'collect_source', lambda *args, **kwargs: rows)
    monkeypatch.setattr(pipeline, 'relevance_filter', lambda items, *args: items)
    monkeypatch.setattr(pipeline, 'merge_event_reports', lambda items, *args: items)
    monkeypatch.setattr(pipeline, 'compact_title', lambda *args: None)
    def summary(item, *args):
        if item.title.endswith(('0', '1')):
            item.summary = ''
    monkeypatch.setattr(pipeline, 'summarize', summary)
    result = pipeline.run(tmp_path, DAY)
    assert result['items'] == 7
    assert result['calls'] == {'search': 0, 'chat': 0}


def test_academic_cap_and_exploration(tmp_path):
    state = State(tmp_path / 'cap.db')
    cfg = {'max_items': 7, 'max_academic_items': 2,
           'windows': {'行业': 3, '学术': 30}, 'exploration_ratio': .1}
    papers = [Article(f'论文{i}', f'https://example.org/p{i}', str(DAY), '测试', category='学术', score=100-i) for i in range(6)]
    news = [Article(f'新闻{i}', f'https://example.org/n{i}', str(DAY), '测试', score=70-i) for i in range(6)]
    picks = select(papers + news, state, DAY, cfg)
    assert len(picks) == 5 and sum(a.category == '学术' for a in picks) == 2
    assert len(select(papers, state, DAY, cfg)) == 2
    state.put('counts', {'total': 100, 'exploration': 0})
    extra = Article('探索论文', 'https://example.org/x', str(DAY), '测试', category='学术', exploration=True, score=100)
    picks = select(papers + news + [extra], state, DAY, cfg)
    assert len(picks) == 5 and sum(a.category == '学术' for a in picks) == 2
    state.close()
