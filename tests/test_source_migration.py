from datetime import date

from brainsong.collect import collect_source, date_text, metadata, parse_listing, url_date
from brainsong.model import Article
from brainsong.pipeline import source_date_report, source_failure_report


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


def test_article_header_date_is_read_before_body_cleanup():
    item = Article('脑电耳机融资', 'https://example.org/news/1', '', '测试')
    metadata(item, '<header><time datetime="2026-09-22">9月22日</time></header><article>正文</article>')
    assert item.published == '2026-09-22'
    assert item.date_evidence == 'article_visible'
    assert '9月22日' not in item.body


def test_bciwiki_group_date_beats_old_policy_date_in_summary():
    spec = {'id': 'bciwiki-policy', 'name': 'BCIwiki·政策',
            'url': 'https://bciwiki.com/section/policy/', 'kind': 'html',
            'links': '.bw-card__title a'}
    html = '''<div class="bw-feed__items"><div class="bw-group">2026 年 9 月</div>
      <article class="bw-card"><div class="bw-card__meta"><span>09-22</span></div>
      <h3 class="bw-card__title"><a href="/item/new-policy/">脑机接口数据新规发布</a></h3>
      <div class="bw-card__summary">这篇文章回顾2025年1月7日的旧政策。</div></article></div>'''
    rows = parse_listing(html, spec)
    assert len(rows) == 1
    assert rows[0].published == '2026-09-22'
    assert rows[0].date_evidence == 'listing'
    assert '2025年1月7日' in rows[0].summary


def test_known_finance_url_dates_filter_navigation_and_old_items():
    east = {'id': 'eastmoney', 'name': '东方财富', 'url': 'https://finance.eastmoney.com/',
            'kind': 'html', 'links': "a[href*='finance.eastmoney.com/a/']"}
    html = '''<a href="https://finance.eastmoney.com/a/czqyw.html">证券要闻导航</a>
      <a href="https://finance.eastmoney.com/a/202609223881086561.html">脑电企业完成融资</a>'''
    rows = parse_listing(html, east)
    assert len(rows) == 1
    assert rows[0].published == '2026-09-22'
    assert rows[0].date_evidence == 'url'
    assert url_date('https://www.chinaventure.com.cn/news/116-20260923-393424.html') == '2026-09-23'
    assert not url_date('https://www.stcn.com/article/detail/4196162.html')


def test_stcn_only_fetches_directly_relevant_article_date():
    spec = {'id': 'stcn', 'name': '证券时报·创投',
            'url': 'https://www.stcn.com/chuangtou/index.html', 'kind': 'html',
            'links': "a[href*='/article/detail/']"}
    class Reader:
        def __init__(self):
            self.observations = []
            self.reads = []
        def get(self, url):
            self.reads.append(url)
            if url == spec['url']:
                return '''<a href="/article/detail/1.html">脑机接口企业完成融资</a>
                          <a href="/article/detail/2.html">无关汽车企业完成融资</a>'''
            return '<header><div class="detail-info">2026-09-23 10:59</div></header><article>正文</article>'
    reader = Reader()
    rows = collect_source(spec, reader, date(2026, 9, 23), [RULE], filter_relevance=False)
    assert reader.reads == [spec['url'], 'https://www.stcn.com/article/detail/1.html']
    assert [row.published for row in rows] == ['2026-09-23', '']
    report = source_date_report('stcn', rows, reader)
    assert report['parsed'] == 2 and report['dated_at_listing'] == 0
    assert report['dated'] == 1 and report['date_rate_pct'] == 0


def test_date_stats_distinguish_old_dated_results_from_missing_dates():
    class Reader:
        observations = []
    old = Article('历史脑电报道', 'https://example.org/old', '2025-01-01', '测试', date_evidence='search')
    missing = Article('新脑电报道', 'https://example.org/new', '', '测试', date_evidence='no_date_hint')
    report = source_date_report('search:行业', [missing], Reader(), [old, missing])
    assert report['parsed'] == 2 and report['dated_at_listing'] == 1
    assert report['returned'] == 1 and report['no_date_hint'] == 1
    assert report['date_rate_pct'] == 50
    assert source_failure_report('audio52', 'failed', ValueError())['date_rate_pct'] is None


def test_wechat_known_links_need_no_credentials(tmp_path):
    from brainsong.collect import collect_wechat
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config/wechat.yaml').write_text('article_urls: []', encoding='utf-8')
    assert collect_wechat(tmp_path, None, date(2026, 9, 17)) == []
