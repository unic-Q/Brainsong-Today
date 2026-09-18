from datetime import date

from brainsong.collect import collect_source, date_text, metadata, parse_listing
from brainsong.model import Article


SPEC = {"id": "test", "name": "测试", "url": "https://example.org/news", "kind": "html", "links": "a[href^='/news/']", "container": ".card"}
RULE = {"id": "eeg", "groups": [["脑电", "EEG"]], "weight": 100}


def test_embedded_english_date():
    assert date_text("Published September 17, 2026 | Author") == "2026-09-17"
    assert date_text("更新于2026年9月17日") == "2026-09-17"


def test_cards_keep_summary_without_borrowing_dates():
    rows = parse_listing('''<section>
      <div class="card"><a href="/news/1"><h2>全新学习状态监测设备上市</h2></a><p>通过脑电反馈改善学习。</p><time datetime="2026-09-17">今天</time></div>
      <div class="card"><a href="/news/2">另一款消费电子产品已经上市</a></div>
    </section>''', SPEC)
    assert rows[0].published == "2026-09-17"
    assert "脑电" in rows[0].summary
    assert rows[1].published == ""


def test_summary_match_not_title_only():
    class Reader:
        observations = []
        def get(self, url):
            return '<article><a href="/news/1">全新学习状态监测设备上市</a><p>脑电反馈功能</p><time>2026-09-17</time></article>'
    items = collect_source(SPEC, Reader(), date(2026, 9, 17), [RULE])
    assert len(items) == 1


def test_jsonld_graph_and_scholarly_date():
    item = Article("脑电研究论文", "https://example.org/paper", "", "测试")
    metadata(item, '''<script type="application/ld+json">{"@graph":[{"@type":["ScholarlyArticle"],"datePublished":"2026-09-17","description":"论文摘要"}]}</script><article>正文</article>''')
    assert item.published == "2026-09-17"
    assert item.summary == "论文摘要"


def test_arxiv_fallback_is_reported_and_normalized():
    class Reader:
        def __init__(self):
            self.observations = []
        def arxiv_api(self, url):
            raise RuntimeError("429")
        def get(self, url):
            assert "rss.arxiv.org" in url
            return '''<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>EEG research</title><id>x</id><link href="http://arxiv.org/abs/2609.12345v2"/><published>2026-09-17T00:00:00Z</published><summary>EEG abstract</summary></entry></feed>'''
    reader = Reader()
    rows = collect_source({"id": "arxiv", "name": "arxiv", "url": "https://export.arxiv.org/api/query", "kind": "arxiv"}, reader, date(2026, 9, 17), [RULE])
    assert rows[0].url == "https://arxiv.org/abs/2609.12345"
    assert reader.observations[0]["fallback"]


def test_finance_detail_dates():
    for cls in ("releaseTime", "detail-info"):
        item = Article("某脑机企业新一轮融资", "https://example.org/news", "", "财经")
        metadata(item, f'<div class="{cls}"><span>2026-09-17 10:52</span></div><article>正文</article>')
        assert item.published == "2026-09-17"


def test_wechat_known_links_need_no_credentials(tmp_path):
    from brainsong.collect import collect_wechat
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config/wechat.yaml').write_text('article_urls: []', encoding='utf-8')
    assert collect_wechat(tmp_path, None, date(2026, 9, 17)) == []
