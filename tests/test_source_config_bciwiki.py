from pathlib import Path

import yaml


def test_specialist_and_search_sources():
    root = Path(__file__).resolve().parents[1]
    rows = yaml.safe_load((root / 'config/sources.yaml').read_text(encoding='utf-8'))
    sources = {s['id']: s for s in rows}
    assert len(sources) == len(rows)
    assert rows[0]['id'] == 'bciwiki'
    assert sources['bciwiki']['kind'] == 'feed'
    assert sources['bciwiki']['url'] == 'https://bciwiki.com/feed/'
    for name in ('stdaily', 'kepuchina', 'deeptech'):
        assert sources[name]['kind'] == 'search'
        assert sources[name]['enabled']
    cfg = yaml.safe_load((root / 'config/brainsong.yaml').read_text(encoding='utf-8'))
    assert cfg['wechat_enabled'] is False
