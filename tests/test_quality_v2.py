from datetime import date
from pathlib import Path
import httpx
import pytest

from brainsong.editor import relevance_filter, summarize, short_text, display_summary, prepare
from brainsong.provider import OfficialGLM, QualityError
from brainsong.model import Article
from brainsong.pipeline import load
from brainsong.state import State
from brainsong.wechat import parse_article, validate_url

ROOT = Path(__file__).resolve().parents[1]
DAY = date(2026, 9, 17)
RULE = {'id':'eeg','weight':100,'groups':[['脑电']],'purpose':'业务相关','direct':True}


def test_low_effort_and_three_item_configuration():
    import json
    def handler(request):
        data = json.loads(request.content)
        assert data['reasoning_effort'] == 'low'
        assert data['max_tokens'] == 4096
        return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':'{}'}}]})
    p = OfficialGLM('fake',client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert p.chat('test',{}) == {}
    assert load(ROOT)[0]['ai']['batch_size'] == 3
    p.client.close()


def article(i=1, **kwargs):
    return Article('脑电耳机研发更新'+str(i), f'https://example.org/{i}', DAY.isoformat(), '测试', matches=[RULE], **kwargs)


def test_partial_failure_retries_only_invalid_rows(tmp_path):
    class AI:
        sizes = []
        def chat(self, task, payload):
            self.sizes.append(len(payload['articles']))
            rows = [{'id':a['id'],'relevance':80,'accept':True,'category':'行业','tags':['脑电']} for a in payload['articles']]
            if len(self.sizes) == 1:
                rows[-1]['relevance'] = 'eighty'
            return {'items':rows}
    ai = AI()
    state = State(tmp_path/'state.db')
    cfg,_,_ = load(ROOT)
    result = relevance_filter([article(1),article(2)],ai,cfg,[RULE],state,DAY)
    assert ai.sizes == [2,1] and len(result) == 2
    assert all(a.relevance == 80 and a.score == 92 for a in result)
    state.close()


def test_truncation_safe_diagnostics():
    p = OfficialGLM('fake',client=httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'choices':[{'finish_reason':'length','message':{'content':'secret raw partial'}}],'usage':{'completion_tokens':4096}}))))
    with pytest.raises(QualityError, match='chat_length'):
        p.chat('test',{})
    assert p.diagnostics[0]['finish_reason'] == 'length'
    assert 'secret' not in str(p.diagnostics)
    p.client.close()


def test_long_existing_summary_only_sent_not_full_body(tmp_path):
    class AI:
        def chat(self, task, payload):
            assert payload['evidence'] == '脑电耳机用于课堂学习状态监测。'*10
            assert 'PRIVATE_BODY' not in str(payload)
            return {'summary':'脑电耳机支持课堂学习状态监测与反馈。'}
    state = State(tmp_path/'state.db')
    a=article(summary='脑电耳机用于课堂学习状态监测。'*10,body='PRIVATE_BODY'*100)
    summarize(a,AI(),state,DAY)
    assert display_summary(a.summary) and a.summary_kind == 'ai'
    state.close()


def test_doc_and_bad_ai_not_displayed(tmp_path):
    class AI:
        def chat(self,*args,**kwargs):
            return {'summary':'doc'}
    state = State(tmp_path/'state.db')
    a=article(summary='doc',body='脑电设备研究的完整正文内容。'*40)
    summarize(a,AI(),state,DAY)
    assert a.summary == '' and a.summary_kind == 'failed'
    state.close()


def test_no_decimal_fragment_selection():
    text='This intentionally long introductory sentence explains the EEG model in more than fifty characters. Accuracy improved from 0.860 to 0.912.'
    result=short_text(text)
    assert result.startswith('This intentionally') and '860 to 0.' not in result
    assert not display_summary('860 to 0.')


def test_summary_100_boundary_and_render_without_50_cut(tmp_path):
    from brainsong.editor import render
    text = '脑电耳机用于教育。' * 11 + '好'
    assert len(text) == 100
    assert display_summary(text)
    assert not display_summary(text + '。')
    class NoAI:
        def chat(self, *args, **kwargs):
            raise AssertionError('Existing valid summary must not call AI')
    state = State(tmp_path/'summary.db')
    a = article(summary=text)
    summarize(a, NoAI(), state, DAY)
    assert a.summary == text
    assert text in render([a], DAY)[1]
    assert a.title in render([a], DAY)[1]
    state.close()


def test_wechat_plain_text_and_strict_host():
    url='https://mp.weixin.qq.com/s/example'
    html='<h1 id="activity-name">脑电耳机产品发布</h1><span id="js_name">测试企业</span><span id="publish_time">2026年9月17日</span><div id="js_content">'+('脑电耳机支持学习与睡眠场景。'*5)+'<img src="https://example.org/private.jpg"/></div>'
    a=parse_article(url,html)
    assert a.published == DAY.isoformat() and a.source == '测试企业'
    assert 'private.jpg' not in a.body
    for invalid in ['https://mp.weixin.qq.com.evil.test/s/a','https://evil.test/?mp.weixin.qq.com','https://mp.weixin.qq.com@evil.test/s/a']:
        with pytest.raises(QualityError): validate_url(invalid)
    with pytest.raises(QualityError): parse_article(url,'<html>请完成验证</html>')


def test_brain_company_synonym_and_eastmoney_date():
    from brainsong.collect import metadata
    _,policy,_=load(ROOT)
    a=Article('脑机公司，拔苗助长','https://example.org/a',DAY.isoformat(),'测试','上市太早。')
    assert prepare(a,policy['rules'],policy['exclude'])
    a=Article('脑电企业融资','https://finance.eastmoney.com/a/123.html','','财经')
    metadata(a,'<div class="infos"><div class="item">2026年09月07日 07:11</div></div><article>正文</article>')
    assert a.published == '2026-09-07'
