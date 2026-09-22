from datetime import date,timedelta
from pathlib import Path
import sqlite3
import pytest
from brainsong.model import Article
from brainsong.editor import select,fill_link_only,render
from brainsong.pipeline import load
from brainsong.state import State

DAY=date(2026,9,20)
ROOT=Path(__file__).resolve().parents[1]


def item(n,category='学术',age=1,score=70,domain='example.org',failed=False):
    return Article(f'脑电消息{n}',f'https://{domain}/{n}',str(DAY-timedelta(days=age)), '媒体',
                   summary='' if failed else '已有可靠的原始摘要，包含已证实的脑电产品信息。',
                   relevance=score,category=category,summary_kind='failed' if failed else 'source')


def test_old_high_score_cannot_evict_recent_within_category(tmp_path):
    cfg=load(ROOT)[0];state=State(tmp_path/'s.db')
    fresh=[item(i,score=65+i) for i in range(3)]
    older=[item(i+10,age=15,score=100,domain='www.nature.com') for i in range(6)]
    other=[item(30,'行业',score=90),item(31,'政策',score=90)]
    picks=select(fresh+older+other,state,DAY,cfg)
    assert len(picks)==5
    assert {a.identity for a in picks if a.category=='学术'}=={a.identity for a in fresh}
    state.close()


def test_same_tier_uses_score_and_old_news_never_fills_vacancies(tmp_path):
    cfg=load(ROOT)[0];state=State(tmp_path/'s.db')
    weak=item(1,age=1,score=60);strong=item(2,age=2,score=95)
    old=item(3,age=20,score=100)
    picks=select([weak,strong,old],state,DAY,cfg)
    assert picks==[strong,weak]
    # Limit one category slot to expose same-tier choice; old fill stays forbidden.
    cfg['max_category_items']=1
    assert select([weak,strong,old],state,DAY,cfg)==[strong]
    state.close()


def test_link_only_only_fills_failed_qualified_items_without_breaking_caps(tmp_path):
    cfg=load(ROOT)[0];state=State(tmp_path/'s.db')
    picks=[item(i,['学术','学术','学术','行业','行业','政策'][i]) for i in range(6)]
    low=item(40,'资本',score=40,failed=True)
    full_category=item(41,'学术',score=100,failed=True)
    valid=item(42,'资本',score=85,failed=True)
    lower=item(43,'资本',score=70,failed=True)
    result=fill_link_only(picks,[low,full_category,lower,valid],state,DAY,cfg)
    assert result==picks+[valid] and not valid.summary
    text=render(result,[],DAY)[1]
    assert valid.url in text and '摘要失败' not in text
    assert fill_link_only(result,[lower],state,DAY,cfg)==result
    state.mark([valid],DAY,'sent')
    assert state.sent(valid)
    state.close()


def test_reset_is_backed_up_one_time_and_next_day_keeps_new_history(tmp_path):
    state=State(tmp_path/'s.db');old=item(1,'政策');new=item(2,'行业')
    state.retain_candidates([old,new],DAY);state.mark([old],DAY,'sent')
    state.put('report:old','sent')
    backup=tmp_path/'backup.db'
    assert state.reset_delivery_once(backup,DAY)
    assert not state.sent(old) and state.candidates()
    with sqlite3.connect(backup) as db:
        assert db.execute('select count(*) from policy_delivered').fetchone()[0]>0
    state.mark([new],DAY,'sent')
    assert not state.reset_delivery_once(backup,DAY+timedelta(days=1))
    assert state.sent(new) and state.get('report:old') is None
    state.close()


def test_reset_does_not_clear_deleted_fingerprints_or_pending_delivery(tmp_path):
    state=State(tmp_path/'s.db');a=item(1)
    state.mark([a],DAY,'pending')
    with pytest.raises(RuntimeError):state.reset_delivery_once(tmp_path/'backup.db',DAY)
    assert state.sent(a) and not (tmp_path/'backup.db').exists()
    state.mark([a],DAY,'sent');state.delete_candidates([a])
    assert state.reset_delivery_once(tmp_path/'backup.db',DAY)
    assert state.deleted(a)
    state.close()


def test_precise_test_delivery_rollback_keeps_prior_history_and_candidates(tmp_path):
    prior_path = tmp_path/'prior.db'
    prior = State(prior_path)
    old, test = item(1, '行业'), item(2, '政策')
    prior.retain_candidates([old, test], DAY)
    prior.mark([old], DAY, 'sent')
    prior.put('counts', {'total': 1, 'exploration': 0})
    prior.put('report:old', 'sent')
    prior.close()
    current = State(tmp_path/'current.db')
    current.retain_candidates([old, test], DAY)
    current.mark([old], DAY, 'sent')
    current.mark([test], DAY, 'sent')
    current.put('counts', {'total': 2, 'exploration': 0})
    current.put('report:old', 'sent')
    current.put('report:test', 'sent')
    result = current.rollback_delivery_to(prior_path, tmp_path/'backup.db', 'run-1')
    assert result['policy'] > 0 and result['reports'] == 1
    assert current.sent(old) and not current.sent(test)
    assert len(current.candidates()) == 2
    assert current.get('counts')['total'] == 1
    assert current.rollback_delivery_to(prior_path, tmp_path/'unused.db', 'run-1')['delivered'] == 0
    current.close()


def test_schedule_is_gated_and_reset_never_scheduled():
    workflow=(ROOT/'.github/workflows/daily.yml').read_text(encoding='utf-8')
    assert '25-55/10 0 * * *' in workflow and '5-55/10 1-3 * * *' in workflow
    assert "vars.BRAINSONG_FORMAL_READY == 'true'" in workflow
    assert "github.event_name == 'workflow_dispatch' && inputs.reset_delivery_once" in workflow
    assert "steps.daily_guard.outputs.should_run == 'true'" in workflow


def test_three_day_profile_is_active_and_reversible():
    cfg=load(ROOT)[0]
    assert cfg['lookback_days'] == 3 and cfg['delivery_interval_days'] == 3
    assert cfg['search_recency'] == 'oneWeek'
    assert set(cfg['operation_profiles']['presets']) == {'three_day', 'daily_30d'}
    assert set(cfg['windows'].values()) == {3}


def test_three_day_delivery_guard(tmp_path):
    state=State(tmp_path/'guard.db')
    assert state.delivery_due(DAY,3)
    state.put('daily-delivery:'+str(DAY),'sent')
    assert not state.delivery_due(DAY+timedelta(days=2),3)
    assert state.delivery_due(DAY+timedelta(days=3),3)
    state.close()
