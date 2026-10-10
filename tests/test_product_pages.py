from datetime import date

from brainsong.editor import apply_assessment, render
from brainsong.model import Article
from brainsong.product_pages import detect_product_launches, enrich_product_pages
from brainsong.state import State

DAY = date(2026, 10, 10)


def product_item():
    item = Article('Neurable 发布 MW75 Neuro 脑电耳机新品',
                   'https://www.neurable.com/newsroom/mw75-neuro',
                   DAY.isoformat(), 'Neurable',
                   summary='Neurable 正式发布 MW75 Neuro 脑电耳机，支持注意力监测。')
    apply_assessment(item, {
        'relevance': 95, 'accept': True, 'category': '行业', 'tags': ['脑电耳机'],
        'companies': ['Neurable'], 'subject': 'Neurable',
        'source_kind': 'original', 'event_type': 'product',
        'event_evidence': 'Neurable 发布 MW75 Neuro 脑电耳机新品',
        'product_name': 'MW75 Neuro',
    })
    item.event_type, item.product_name = 'product', 'MW75 Neuro'
    return item


def test_product_page_link_only_after_official_page_verification(tmp_path):
    item = product_item()
    state = State(tmp_path / 'state.db')
    calls = []

    class Provider:
        def search(self, query, domain='', recency='oneMonth'):
            calls.append((query, recency))
            return [
                {'link': 'https://www.amazon.com/mw75-neuro', 'title': 'MW75 Neuro'},
                {'link': 'https://www.neurable.com/newsroom/mw75-neuro-review',
                 'title': 'MW75 Neuro 评测'},
                {'link': 'https://www.neurable.com/products/mw75-neuro',
                 'title': 'MW75 Neuro | Neurable'},
            ]

    class Reader:
        def get(self, url):
            assert url == 'https://www.neurable.com/products/mw75-neuro'
            return '<title>MW75 Neuro | Neurable</title><h1>MW75 Neuro</h1>'

    sources = [{'company': 'Neurable', 'url': 'https://www.neurable.com/newsroom'}]
    enrich_product_pages([item], sources, Provider(), Reader(), state, DAY)
    assert calls == [('Neurable MW75 Neuro 官方 产品页', '')]
    assert item.product_url == 'https://www.neurable.com/products/mw75-neuro'
    assert '[原文](' in render([item], DAY)[1]
    assert '[查看产品页](https://www.neurable.com/products/mw75-neuro)' in render([item], DAY)[1]
    enrich_product_pages([item], sources, Provider(), Reader(), state, DAY)
    assert len(calls) == 1  # The verified page is cached.
    state.close()


def test_unverified_or_unmatched_result_never_gets_product_link(tmp_path):
    item = product_item()
    state = State(tmp_path / 'state.db')

    class Provider:
        def search(self, *args, **kwargs):
            return [{'link': 'https://www.neurable.com/products/another-headset',
                     'title': '另一款耳机'}]

    class Reader:
        def get(self, url):
            return '<title>另一款耳机</title><h1>Another Headset</h1>'

    enrich_product_pages([item], [], Provider(), Reader(), state, DAY)
    assert not item.product_url
    assert '查看产品页' not in render([item], DAY)[1]
    state.close()


def test_chinese_brand_can_use_official_search_result_when_page_names_company(tmp_path):
    item = Article('脑颂科技发布听心一号脑电耳机',
                   'https://news.example.org/brain-headset', DAY.isoformat(), '行业媒体',
                   summary='脑颂科技发布听心一号脑电耳机。', event_type='product',
                   product_name='听心一号', companies=['脑颂科技'], subject='脑颂科技')
    state = State(tmp_path / 'state.db')

    class Provider:
        def search(self, *args, **kwargs):
            return [{'link': 'https://brainsong.example/products/tingxin-one',
                     'media': '脑颂科技官网', 'title': '听心一号'}]

    class Reader:
        def get(self, url):
            return '<title>听心一号</title><h1>听心一号</h1><footer>脑颂科技</footer>'

    enrich_product_pages([item], [], Provider(), Reader(), state, DAY)
    assert item.product_url == 'https://brainsong.example/products/tingxin-one'
    state.close()


def test_only_genuine_named_product_launch_triggers_search(tmp_path):
    item = product_item()
    state = State(tmp_path / 'state.db')

    class Provider:
        def search(self, *args, **kwargs):
            raise AssertionError('No product search expected')

    item.event_type = 'ordinary'
    enrich_product_pages([item], [], Provider(), object(), state, DAY)
    item.event_type = 'product'
    item.product_name = ''
    enrich_product_pages([item], [], Provider(), object(), state, DAY)
    assert '查看产品页' not in render([item], DAY)[1]
    state.close()


def test_assessment_no_longer_judges_every_articles_product_launch():
    item = product_item()
    row = {'relevance': 95, 'accept': True, 'category': '行业', 'tags': [],
           'event_type': 'product', 'source_kind': 'original',
           'event_evidence': 'Neurable 发布 MW75 Neuro 脑电耳机新品',
           'product_name': 'Imaginary 9000'}
    apply_assessment(item, row)
    assert item.event_type == 'ordinary' and not item.product_name


def test_release_word_in_summary_triggers_body_based_product_detection(tmp_path):
    item = Article('Neurable 有新进展', 'https://www.neurable.com/newsroom/new',
                   DAY.isoformat(), 'Neurable',
                   summary='Neurable 发布一款用于注意力监测的新耳机。',
                   body='该公司正式推出 MW75 Neuro 脑电耳机，支持注意力监测。',
                   companies=['Neurable'], subject='Neurable')
    state = State(tmp_path / 'state.db')
    calls = []

    class Provider:
        def chat(self, task, payload):
            calls.append(payload)
            return {'items': [{'id': 'P0', 'is_product_launch': True,
                               'company': 'Neurable', 'product_name': 'MW75 Neuro'}]}

    detect_product_launches([item], Provider(), state, DAY)
    assert item.event_type == 'product' and item.product_name == 'MW75 Neuro'
    assert len(calls) == 1 and 'MW75 Neuro' in calls[0][0]['source_evidence']
    detect_product_launches([item], Provider(), state, DAY)
    assert len(calls) == 1  # Same source evidence uses the cached judgement.
    state.close()


def test_no_release_word_in_summary_means_no_launch_judgement(tmp_path):
    item = Article('Neurable 新品', 'https://www.neurable.com/newsroom/new',
                   DAY.isoformat(), 'Neurable', summary='Neurable 的新耳机具有注意力监测功能。',
                   body='正文写了该公司发布 MW75 Neuro。',
                   event_type='product', product_name='MW75 Neuro')
    state = State(tmp_path / 'state.db')

    class Provider:
        def chat(self, *args):
            raise AssertionError('No launch judgement expected')

    detect_product_launches([item], Provider(), state, DAY)
    assert item.event_type == 'ordinary' and not item.product_name
    state.close()


def test_paper_release_is_not_a_product_and_hallucinated_name_is_rejected(tmp_path):
    item = Article('研究团队发布脑电论文', 'https://example.org/paper',
                   DAY.isoformat(), '学术来源', summary='研究团队发布脑电论文。',
                   body='研究团队公开了神经解码算法。', subject='研究团队')
    state = State(tmp_path / 'state.db')

    class Provider:
        def chat(self, task, payload):
            return {'items': [{'id': 'P0', 'is_product_launch': True,
                               'company': '研究团队', 'product_name': 'MW75 Neuro'}]}

    detect_product_launches([item], Provider(), state, DAY)
    assert item.event_type == 'ordinary' and not item.product_name
    state.close()
