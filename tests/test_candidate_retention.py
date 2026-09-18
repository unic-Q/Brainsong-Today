from datetime import date, timedelta

from brainsong.model import Article
from brainsong.state import State


def test_original_summary_survives_failed_generation_and_prune(tmp_path):
    day = date(2026, 9, 17)
    state = State(tmp_path / 'test.db')
    item = Article('脑电耳机产品发布', 'https://example.org/a', str(day), '测试',
                   summary='原始文章摘要，包含耳机产品特征和发布消息。')
    original = item.summary
    state.retain_candidates([item], day)
    item.summary = ''
    state.save(item)
    state.prune(day + timedelta(days=40))
    assert state.candidates()[0].summary == original
    state.release_candidates(day + timedelta(days=40))
    state.prune(day + timedelta(days=69))
    assert len(state.candidates()) == 1
    state.prune(day + timedelta(days=71))
    assert not state.candidates()
    state.close()


def test_checkpoint_survives_restart_and_missing_date(tmp_path):
    path = tmp_path / 'test.db'
    state = State(path)
    item = Article('待补日期的脑电资讯', 'https://example.org/a', '', '测试', summary='公开候选摘要，暂时没有发布日期。')
    state.retain_candidates([item], date(2026, 9, 17))
    state.close()
    state = State(path)
    assert state.candidates()[0].published == ''
    assert state.candidates()[0].summary == item.summary
    state.close()


def test_event_merge_keeps_official_and_sources(tmp_path):
    from brainsong.editor import merge_event_reports
    state = State(tmp_path / 'dedup.db')
    day = date(2026, 9, 17)
    official = Article('标准公告', 'https://www.nmpa.gov.cn/a', str(day), '药监局', category='政策')
    report = Article('大学参与起草该标准', 'https://example.org/a', str(day), '媒体', category='政策')
    class AI:
        def chat(self, *args):
            return {'groups': [['A0', 'A1']]}
    rows = merge_event_reports([report, official], AI(), state, day)
    assert rows == [official]
    assert len(official.sources) == 2
    state.close()
