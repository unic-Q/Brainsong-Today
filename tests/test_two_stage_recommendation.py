from datetime import date, timedelta

import pytest

from brainsong.model import Article, effective_relevance, ranking_score, shortlist_score, star_text, source_factor
from brainsong.editor import select, render, summarize, apply_assessment
from brainsong.state import State

DAY = date(2026, 9, 20)


@pytest.mark.parametrize('score,stars', [(100,'★★★★★'), (90,'★★★★☆'), (80,'★★★★'), (70,'★★★☆'), (75,'★★★★'), (0,''), (65,'★★★☆')])
def test_half_stars_without_padding(score, stars):
    assert star_text(score) == stars


def test_two_stages_never_multiply_relevance_twice(tmp_path):
    cfg = {'max_items':7, 'shortlist_items':10, 'windows':dict.fromkeys(['行业','资本','政策','学术'],30),
           'scoring':{'source_domains':{'strong.test':[100,100], 'weak.test':[50,50]}}}
    state = State(tmp_path/'state.db')
    rows = [Article(f'主体{i}发布进展', f'https://weak.test/{i}', str(DAY), '媒体',
                    relevance=100-i, category=['行业','资本','政策','学术'][i%4]) for i in range(10)]
    rows[9].url = 'https://strong.test/9'
    late = Article('高权威但已过时的候选', 'https://strong.test/late', str(DAY-timedelta(days=14)), '媒体', relevance=100)
    picks = select(rows+[late], state, DAY, cfg)
    assert len(picks) == 7 and picks[0] is rows[9]
    assert late not in picks  # Not in the first ten, despite second-stage score 100.
    assert rows[9].recommendation_score == 91
    assert shortlist_score(rows[9], DAY) == 91
    assert ranking_score(rows[9], DAY+timedelta(days=10), cfg['scoring']) == 91
    assert '推荐指数：★★★★☆' in render(picks, [], DAY)[1]
    state.close()


def test_business_category_weight_applies_to_gate_and_both_stages():
    scoring = {'category_weights': {'行业':1.2, '资本':1.2, '政策':1.2, '学术':1.0},
               'source_domains': {'strong.test':[100,100]}}
    capital = Article('消费脑电企业融资', 'https://strong.test/c', str(DAY), '媒体', relevance=50, category='资本')
    academic = Article('脑电论文', 'https://strong.test/a', str(DAY), '期刊', relevance=50, category='学术')
    assert effective_relevance(capital, scoring) == 60
    assert shortlist_score(capital, DAY, scoring) == 60
    assert ranking_score(capital, DAY, scoring) == 60
    assert shortlist_score(academic, DAY, scoring) == 50
    assert ranking_score(academic, DAY, scoring) == 50
    apply_assessment(capital, {'relevance':50, 'accept':True, 'category':'资本', 'tags':[]}, scoring)
    assert capital.accepted
    apply_assessment(capital, {'relevance':90, 'accept':False, 'category':'资本', 'tags':[]}, scoring)
    assert not capital.accepted


def test_business_category_weight_is_capped_at_100():
    scoring = {'category_weights': {'行业':1.2}}
    item = Article('行业高分事件', 'https://example.org/high', str(DAY), '媒体', relevance=95, category='行业')
    assert effective_relevance(item, scoring) == 100


def test_eeg_papers_without_emotion_recognition_get_only_a_small_discount():
    scoring = {'academic_eeg_without_emotion_factor': .95,
               'source_domains': {'strong.test': [100, 100]}}
    plain = Article('脑电信号处理研究', 'https://strong.test/plain', str(DAY), '期刊',
                    relevance=80, category='学术')
    emotion = Article('脑电情绪识别研究', 'https://strong.test/emotion', str(DAY), '期刊',
                      relevance=80, category='学术')
    english = Article('EEG emotion recognition study', 'https://strong.test/english', str(DAY), '期刊',
                      relevance=80, category='学术')
    industry = Article('脑电产品发布', 'https://strong.test/product', str(DAY), '媒体',
                       relevance=80, category='行业')
    assert effective_relevance(plain, scoring) == 76
    assert shortlist_score(plain, DAY, scoring) == 76
    assert ranking_score(plain, DAY, scoring) == 76
    assert effective_relevance(emotion, scoring) == 80
    assert effective_relevance(english, scoring) == 80
    assert effective_relevance(industry, scoring) == 80


def test_policy_scope_keeps_china_and_rejects_foreign_policy():
    row = {'relevance':90, 'accept':True, 'category':'政策', 'tags':[]}
    domestic = Article('国家药监局发布脑机接口标准', 'https://www.nmpa.gov.cn/policy', str(DAY), '国家药监局')
    foreign = Article('US regulator publishes neural data rules', 'https://www.fda.gov/policy', str(DAY), 'FDA')
    apply_assessment(domestic, row, None, 'china')
    apply_assessment(foreign, row, None, 'china')
    assert domestic.accepted
    assert not foreign.accepted


def test_source_domain_matching_and_preprint_rating():
    cfg={'source_domains':{'nature.com':[95,95]}}
    original=Article('论文','https://www.nature.com/a',str(DAY),'期刊')
    spoof=Article('论文','https://nature.com.evil.test/a',str(DAY),'期刊')
    preprint=Article('论文','https://arxiv.org/abs/123',str(DAY),'arXiv')
    assert source_factor(original,cfg)==.95
    assert source_factor(spoof,cfg)==.57
    assert source_factor(preprint,cfg)==.745


def test_existing_summary_subject_front_no_extra_ai(tmp_path):
    state=State(tmp_path/'state.db')
    a=Article('某公司发布产品','https://example.org/a',str(DAY),'媒体',summary='发布的新设备支持脑电采集，用于教育研究。', subject='某公司')
    class NoAI:
        def chat(self,*args,**kwargs):
            pytest.fail('Existing short source summary should be reused')
    summarize(a,NoAI(),state,DAY)
    assert a.summary.startswith('某公司：') and len(a.summary)<=100
    assert not a.source_summary.startswith('某公司：')
    state.retain_candidates([a], DAY)
    assert not state.candidates()[0].source_summary.startswith('某公司：')
    state.close()


def test_subject_cannot_be_invented():
    a=Article('清华大学团队发布研究','https://example.org/a',str(DAY),'媒体')
    row={'relevance':90,'accept':True,'category':'学术','tags':[],'subject':'不存在的教授'}
    apply_assessment(a,row)
    assert a.subject==''
    apply_assessment(a,dict(row,subject='清华大学团队'))
    assert a.subject=='清华大学团队'
