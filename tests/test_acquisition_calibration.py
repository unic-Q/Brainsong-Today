from datetime import date
from pathlib import Path

from brainsong.acquisition import business_context, estimated_relevance
from brainsong.editor import prepare, category_hint, select, relevance_filter
from brainsong.pipeline import load, direction_batch
from brainsong.model import Article
from brainsong.state import State

ROOT = Path(__file__).resolve().parents[1]
DAY = date(2026,9,20)


def item(title, url='https://example.org/a', day='2026-09-20', summary=''):
    return Article(title,url,day,'来源',summary=summary)


def test_business_gate_blocks_unrelated_despite_complete_evidence():
    cfg,policy,_=load(ROOT)
    for title in ('烟花爆竹全链条监管意见','通用机器人芯片公司融资10亿元','普通教育教学政策'):
        a=item(title,summary='完整材料，权威媒体发布，最新信息。')
        prepare(a,policy['rules'],policy['exclude'])
        assert not business_context(a,cfg)
    for title in ('全新企业发布耳周EEG数据接口','课堂耳机支持听觉训练','某省脑机接口专项落地补贴','Nimbus Studio SDK release'):
        a=item(title)
        prepare(a,policy['rules'],policy['exclude'])
        assert business_context(a,cfg)


def test_quality_and_authority_outrank_evidence_convenience():
    cfg,_,_=load(ROOT)
    high=item('脑电行业标准','https://www.nmpa.gov.cn/a')
    low=item('脑电普通报道',summary='已经具备完整摘要内容，可以直接阅读。')
    assert direction_batch([low,high],[],cfg,lambda a:90 if a is high else 70,1)==[high]
    assert direction_batch([low,high],[],cfg,lambda a:90,1)==[high]


def test_today_gets_analysis_and_shortlist_before_older_items(tmp_path):
    cfg,_,_=load(ROOT)
    new=item('脑电新进展')
    old=item('脑电旧进展','https://example.org/old','2026-09-19')
    assert direction_batch([old,new],[],cfg,lambda a:100 if a is old else 60,1)==[new]
    rows=[Article(f'今日合格进展{i}',f'https://example.org/{i}',str(DAY),'媒体',
                  relevance=60,category=['行业','资本','政策','学术'][i%4]) for i in range(10)]
    old.relevance=100
    state=State(tmp_path/'state.db')
    picks=select(rows+[old],state,DAY,cfg)
    assert len(picks)==7 and old not in picks
    state.close()


def test_calibration_examples_reach_model_and_change_cache_signature(tmp_path):
    cfg,policy,_=load(ROOT)
    assert [x['score'] for x in cfg['relevance_examples']]==[80,50,0,40,100,50,80,90,85,30,10,100,70]
    class AI:
        calls=0
        def chat(self,task,payload):
            self.calls+=1
            assert '用户最新相关性评分锚点' in payload['company_profile']
            assert '不能再一概拒绝地方政策' in task
            return {'items':[{'id':a['id'],'relevance':70,'accept':True,'category':'政策','tags':['脑机']} for a in payload['articles']]}
    ai=AI(); state=State(tmp_path/'cache.db'); a=item('脑机产业专项补贴政策')
    prepare(a,policy['rules'],policy['exclude'])
    relevance_filter([a],ai,cfg,policy['rules'],state,DAY)
    relevance_filter([a],ai,cfg,policy['rules'],state,DAY)
    assert ai.calls==1
    cfg['relevance_examples'][11]['score']=75
    relevance_filter([a],ai,cfg,policy['rules'],state,DAY)
    assert ai.calls==2
    state.close()


def test_paper_hint_and_noninvasive_financing_are_not_misclassified():
    cfg,policy,_=load(ROOT)
    a=item('单通道脑电解码研究，准确率提升')
    assert category_hint(a)=='学术'
    for title,expected in [('非侵入式脑电耳机公司融资5000万元',80),('植入式脑机公司融资5亿元',50)]:
        a=item(title); prepare(a,policy['rules'],policy['exclude'])
        assert estimated_relevance(a,cfg)==expected


def test_emotion_recognition_is_positive_in_chinese_and_english():
    cfg, policy, _ = load(ROOT)
    for title in ('脑电情绪识别模型发表论文', 'EEG emotion recognition model study'):
        paper = item(title)
        assert prepare(paper, policy['rules'], policy['exclude'])
        assert any(rule['id'] == 'r17-情绪识别' for rule in paper.matches)
        assert estimated_relevance(paper, cfg) == 85
