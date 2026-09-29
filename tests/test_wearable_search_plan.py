from collections import Counter
from datetime import date, timedelta
from pathlib import Path

from brainsong.acquisition import business_context
from brainsong.model import Article
from brainsong.pipeline import load, queries
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
    assert all(' OR ' not in query and len(query) <= 55 for _, query in first)
    assert all('FDA' not in query for _, query in first)
    assert any('脑电眼镜' in query for topic, query in first if topic == '眼镜可穿戴')
    assert any('AI眼镜' in query for topic, query in first if topic == '眼镜可穿戴')


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
