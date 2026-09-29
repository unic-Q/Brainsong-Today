from datetime import date
from brainsong.model import Article
from brainsong.editor import merge, summarize
from brainsong.state import State

DAY = date(2026, 9, 17)


def test_ai_never_overwrites_candidate_evidence(tmp_path):
    s = State(tmp_path/'test.db')
    a = Article('脑电产品', 'https://example.org/a', str(DAY), '测试', summary='原始证据材料。'*30)
    original = a.summary
    s.retain_candidates([a], DAY)
    a.summary = '旧模型摘要。'*10
    a.summary_kind = 'ai'
    s.retain_candidates([a], DAY)
    stored = s.candidates()[0]
    assert stored.summary == original and stored.source_summary == original
    assert stored.summary_kind == 'source'
    s.close()


def test_old_ai_summary_is_not_source_or_reused(tmp_path):
    s = State(tmp_path/'test.db')
    a = Article('脑电产品', 'https://example.org/a', str(DAY), '测试',
                summary='这段是旧模型生成的简短摘要。', summary_kind='ai', body='原始脑电材料。'*30)
    class AI:
        calls = 0
        def chat(self, task, payload):
            self.calls += 1
            assert '旧模型' not in payload['evidence']
            return {'summary':'脑电产品更新，详细参数仍需进一步验证。'}
    ai = AI()
    summarize(a, ai, s, DAY)
    assert ai.calls == 1 and a.summary_version == '100-v6-undated-safety'
    summarize(a, ai, s, DAY)
    assert ai.calls == 1
    s.close()


def test_merge_does_not_import_legacy_ai_as_original():
    raw = Article('脑电产品', 'https://example.org/a', str(DAY), '测试', body='真实原文。')
    old = Article('脑电产品', raw.url, str(DAY), '测试', summary='旧AI摘要比原文长很多。'*3, summary_kind='ai')
    result = merge([raw, old])[0]
    assert not result.summary and not result.source_summary
    assert result.body == '真实原文。'


def test_arxiv_uses_abstract_not_page_description():
    from brainsong.collect import metadata
    a = Article('论文', 'https://arxiv.org/abs/2609.17886', str(DAY), 'arXiv')
    metadata(a, '<meta name="description" content="Abstract page for arXiv paper test"><blockquote class="abstract"><span>Abstract:</span>Actual EEG dataset research results.</blockquote>')
    assert a.summary == 'Actual EEG dataset research results.'


def test_overlong_summary_retries_with_original_evidence(tmp_path):
    state = State(tmp_path/'test.db')
    a = Article('脑电更新', 'https://example.org/a', str(DAY), '测试', summary='原始材料说明脑电产品进展。'*30)
    class AI:
        calls = 0
        def chat(self, task, payload):
            self.calls += 1
            assert '原始材料' in payload['evidence']
            if self.calls == 1:
                return {'summary':'脑电设备用于研究场景。'*15}
            assert len(payload['draft']) > 100
            return {'summary':'脑电产品取得进展，目前仍需在研究场景中进一步验证。'}
    ai=AI()
    summarize(a, ai, state, DAY)
    assert ai.calls == 2 and a.summary_kind == 'ai' and len(a.summary)<=100
    assert a.source_summary.startswith('原始材料')
    state.close()
