from collections import Counter
from datetime import date, timedelta
from pathlib import Path

from brainsong.acquisition import business_context
from brainsong.model import Article
from brainsong.pipeline import load, queries, search_direction, search_site, site_search_ids
from repo_courier.matching import match_rules


ROOT = Path(__file__).resolve().parents[1]
DAY = date(2026, 9, 29)


def test_each_live_search_direction_has_four_fixed_and_one_rotating_query():
    cfg, policy, _ = load(ROOT)
    first = queries(policy['rules'], cfg, DAY)
    next_day = queries(policy['rules'], cfg, DAY + timedelta(days=1))
    assert Counter(topic for topic, _ in first) == dict.fromkeys(
        ('核心', '耳机教育', '企业', '资本', '政策', '眼镜可穿戴'), 5)
    assert '学术' not in {topic for topic, _ in first}
    for topic in ('核心', '耳机教育', '企业', '资本', '政策', '眼镜可穿戴'):
        today_queries = [query for direction, query in first if direction == topic]
        tomorrow_queries = [query for direction, query in next_day if direction == topic]
        assert today_queries[:4] == tomorrow_queries[:4]
        assert today_queries[4] != tomorrow_queries[4]
    assert all(len(query) <= 70 for _, query in first)
    assert '脑电大模型 OR EEG大模型 OR EEG foundation model' in [
        query for topic, query in first if topic == '核心']
    assert '耳周脑电 OR ear-EEG' in [query for topic, query in first if topic == '核心']
    assert all('FDA' not in query for _, query in first)
    assert any('脑电眼镜' in query for topic, query in first if topic == '眼镜可穿戴')
    assert any('AI眼镜' in query for topic, query in first if topic == '眼镜可穿戴')


def test_eight_site_searches_alternate_as_two_disjoint_fours():
    cfg, _, sources = load(ROOT)
    today = site_search_ids(sources, DAY)
    tomorrow = site_search_ids(sources, DAY + timedelta(days=1))
    configured = {source['id'] for source in sources
                  if source.get('enabled', True) and source['kind'] == 'search'}
    assert cfg['site_search_limit'] == 4
    assert len(today) == len(tomorrow) == 4
    assert today.isdisjoint(tomorrow)
    assert today | tomorrow == configured
    assert site_search_ids(sources, DAY + timedelta(days=2)) == today


def test_one_day_search_widens_only_when_dated_results_are_scarce():
    cfg, _, _ = load(ROOT)
    class Search:
        def __init__(self, fresh):
            self.calls = []
            self.fresh = fresh
        def search(self, query, domain='', recency='oneMonth'):
            self.calls.append((query, domain, recency))
            if recency == 'oneDay':
                return [dict(title=f'脑电新品{i}', link=f'https://example.com/{query}/{i}',
                             publish_date=DAY.isoformat(), content='消费级脑电产品')
                        for i in range(self.fresh)]
            return [dict(title='脑电新品补充', link=f'https://example.com/{query}/older',
                         publish_date=(DAY - timedelta(days=2)).isoformat(), content='消费级脑电产品')]
    enough = Search(2)
    batches, errors = search_direction(enough, '核心', ['甲', '乙', '丙'], cfg, DAY)
    assert not errors and len(batches) == 3
    assert [call[2] for call in enough.calls] == ['oneDay'] * 3
    scarce = Search(0)
    batches, errors = search_direction(scarce, '核心', ['甲', '乙', '丙'], cfg, DAY)
    assert not errors and len(batches) == 5
    assert [call[2] for call in scarce.calls] == ['oneDay'] * 3 + ['oneWeek'] * 2


def test_identical_queries_reuse_results_within_one_run():
    cfg, _, _ = load(ROOT)
    class Search:
        def __init__(self): self.calls = []
        def search(self, query, domain='', recency='oneMonth'):
            self.calls.append((query, recency))
            return [dict(title='脑电新品', link='https://example.com/one',
                         publish_date=DAY.isoformat(), content='消费级脑电产品'),
                    dict(title='脑电新品二', link='https://example.com/two',
                         publish_date=DAY.isoformat(), content='消费级脑电产品')]
    provider, cache = Search(), {}
    search_direction(provider, '核心', ['脑电耳机'], cfg, DAY, cache)
    search_direction(provider, '企业', ['脑电耳机'], cfg, DAY, cache)
    assert provider.calls == [('脑电耳机', 'oneDay')]


def test_site_search_widens_once_and_never_counts_old_results_as_fresh():
    cfg, _, sources = load(ROOT)
    site = next(s for s in sources if s['id'] == 'stdaily')
    class Search:
        def __init__(self): self.calls = []
        def search(self, query, domain='', recency='oneMonth'):
            self.calls.append(recency)
            day = DAY - timedelta(days=8 if recency == 'oneDay' else 1)
            return [dict(title='脑机接口新品', link='https://www.stdaily.com/news/1',
                         publish_date=day.isoformat(), content='消费级脑机接口')]
    provider = Search()
    _, collected, errors = search_site(provider, site, cfg, DAY)
    assert not errors and provider.calls == ['oneDay', 'oneWeek']
    assert len(collected) == 1 and collected[0].published == (DAY - timedelta(days=1)).isoformat()


def test_wearable_aliases_match_without_admitting_generic_watch():
    cfg, policy, _ = load(ROOT)
    rules = policy['rules']
    eeg_glasses = Article('EEG smart glasses发布', 'https://example.com/glasses', DAY.isoformat(), '媒体')
    ai_glasses = Article('AI 眼镜发布教育功能', 'https://example.com/ai-glasses', DAY.isoformat(), '媒体')
    eeg_earbuds = Article('EEG earbuds支持睡眠监测', 'https://example.com/earbuds', DAY.isoformat(), '媒体')
    watch = Article('普通智能手表新增AI表盘', 'https://example.com/watch', DAY.isoformat(), '媒体')
    assert 'r60-脑电眼镜' in {rule['id'] for rule in match_rules(eeg_glasses.title, '', '', rules)}
    assert 'r63-AI眼镜新品' in {rule['id'] for rule in match_rules(ai_glasses.title, '', '', rules)}
    assert 'r04-脑电耳机' in {rule['id'] for rule in match_rules(eeg_earbuds.title, '', '', rules)}
    assert business_context(eeg_glasses, cfg)
    assert business_context(ai_glasses, cfg)
    assert not business_context(watch, cfg)
