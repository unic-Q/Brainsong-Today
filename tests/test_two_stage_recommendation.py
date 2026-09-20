from datetime import date, timedelta

import pytest

from brainsong.model import Article, ranking_score, shortlist_score, star_text, source_factor
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
