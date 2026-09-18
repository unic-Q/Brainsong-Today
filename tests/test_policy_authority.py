from datetime import date, timedelta

from brainsong.model import Article, policy_authority, ranking_score
from brainsong.editor import select
from brainsong.state import State

DAY = date(2026, 9, 17)


def item(url, age=100):
    return Article('脑电数据集标准 YY/T2029—2026', url, str(DAY-timedelta(days=age)),
                   '来源', category='政策', score=94)


def test_authority_and_window():
    national = item('https://www.nmpa.gov.cn/a')
    assert policy_authority(national) == 1
    assert ranking_score(national, DAY) == 0
    assert not national.in_window(DAY, {'政策':30})
    local = item('https://www.beijing.gov.cn/a')
    assert policy_authority(local) == .8
    assert ranking_score(local, DAY) == 0
    assert not local.in_window(DAY, {'政策':30})
    assert policy_authority(item('https://www.sznews.com/a')) == .8
    assert policy_authority(item('https://bciwiki.com/a')) == .7
    assert policy_authority(item('https://www.nmpa.gov.cn.evil.test/a')) == .7


def test_national_original_promotes_reprint():
    a = item('https://bciwiki.com/a', age=15)
    a.sources.append({'name':'原始公告','url':'https://www.nmpa.gov.cn/a'})
    assert policy_authority(a) == 1 and a.in_window(DAY, {'政策':30})


def test_policy_thirty_day_boundary_with_decay():
    for age, factor in ((0, 1), (3, .45), (7, .2), (29, .05)):
        a = item('https://www.nmpa.gov.cn/a', age=age)
        assert a.in_window(DAY, {'政策':30})
        assert ranking_score(a, DAY) == round(94 * factor, 2)
    for age in (-1, 30, 100):
        assert not item('https://www.nmpa.gov.cn/a', age=age).in_window(DAY, {'政策':30})


def test_policy_delivery_survives_year_and_title_changes(tmp_path):
    state = State(tmp_path/'state.db')
    a = item('https://www.nmpa.gov.cn/a')
    state.save(a)
    state.mark([a], DAY, 'sent')
    state.prune(DAY+timedelta(days=500))
    b = item('https://example.org/reprint')
    b.title = '大学参与起草 YY/T 2029-2026 行业标准'
    assert state.sent(b)
    assert len(state.recent(DAY+timedelta(days=500))) == 1
    cfg = {'max_items':7, 'windows':{'政策':30},'exploration_ratio':.1}
    assert select([a], state, DAY, cfg) == []
    state.close()
