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
    state.prune(day + timedelta(days=29))
    assert state.candidates()[0].summary == original
    state.prune(day + timedelta(days=30))
    assert not state.candidates()
    state.retain_candidates([item], day + timedelta(days=31))
    state.save(item)
    assert not state.candidates() and state.deleted(item)
    assert state.cached(item) is None
    state.close()


def test_manual_delete_blocks_changed_title_and_tracking_url_after_restart(tmp_path):
    state = State(tmp_path/'deleted.db')
    a = Article('删除的脑电新闻', 'https://example.org/a', '2026-09-20', '媒体', summary='原始资料。')
    state.retain_candidates([a], date(2026,9,20))
    state.save(a)
    state.delete_candidates([a])
    state.close()
    state = State(tmp_path/'deleted.db')
    b = Article('更改后的标题', 'https://example.org/a?utm_source=news', '2026-09-21', '媒体')
    state.retain_candidates([b], date(2026,9,21))
    state.save(b)
    assert state.deleted(b) and not state.candidates() and not state.recent(date(2026,9,21))
    state.prune(date(2030,1,1))
    assert state.deleted(b)
    state.close()


def test_undated_and_policy_candidates_expire_but_delivery_history_remains(tmp_path):
    state = State(tmp_path/'expiry.db')
    day = date(2026,9,20)
    a = Article('无日期脑电新闻','https://example.org/a','','媒体')
    b = Article('脑电政策','https://example.org/b',str(day),'政府',category='政策')
    state.retain_candidates([a,b],day)
    state.mark([b],day,'sent')
    state.prune(day+timedelta(days=30))
    assert not state.candidates() and state.deleted(a) and state.deleted(b)
    assert state.sent(b)
    state.close()


def test_three_day_prune_removes_old_and_undated_copies_before_next_run(tmp_path):
    state = State(tmp_path / 'three-day.db')
    today = date(2026, 9, 29)
    current = Article('今日脑电耳机发布', 'https://example.org/current', '2026-09-29', '媒体')
    boundary = Article('前天脑电政策', 'https://example.org/boundary', '2026-09-27', '政府', category='政策')
    old = Article('三天前脑电新闻', 'https://example.org/old', '2026-09-26', '媒体')
    undated = Article('未注明日期的脑电新闻', 'https://example.org/undated', '', '媒体')
    state.retain_candidates([current, boundary, old, undated], today)
    for item in (current, boundary, old, undated):
        state.save(item)
    state.mark([old], today, 'sent')

    state.prune(today, 3, drop_undated=True, use_first_reported=True)

    assert {item.identity for item in state.candidates()} == {current.identity, boundary.identity}
    assert state.deleted(old) and state.deleted(undated)
    assert state.sent(old)
    assert state.cached(old) is None and state.cached(undated) is None
    state.close()


def test_three_day_prune_uses_first_reported_for_strict_freshness(tmp_path):
    state = State(tmp_path / 'first-reported.db')
    today = date(2026, 9, 29)
    repeated = Article('旧闻重新刊载', 'https://example.org/reprint', '2026-09-29', '媒体',
                       first_reported='2026-09-24')
    state.retain_candidates([repeated], today)
    state.prune(today, 3, drop_undated=True, use_first_reported=True)
    assert state.deleted(repeated) and not state.candidates()
    state.close()


def test_prune_only_cli_does_not_collect_or_send(tmp_path, monkeypatch, capsys):
    import sys
    from datetime import datetime
    from pathlib import Path
    from zoneinfo import ZoneInfo
    from brainsong import cli

    today = datetime.now(ZoneInfo('Asia/Shanghai')).date()
    db = tmp_path / 'maintenance.db'
    state = State(db)
    undated = Article('无日期脑电候选', 'https://example.org/no-date', '', '媒体')
    state.retain_candidates([undated], today)
    state.close()
    monkeypatch.setattr(cli, 'run', lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError('maintenance must not collect or send')))
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setattr(sys, 'argv', ['brainsong-today', '--root', str(root),
                                      '--state', str(db), '--prune-only'])
    cli.main()
    assert '"removed": 1' in capsys.readouterr().out
    state = State(db)
    assert not state.candidates() and state.deleted(undated)
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
