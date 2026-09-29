"""Reformat existing local candidates. Paid summaries/headlines; no search/send."""
import argparse
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from brainsong.editor import compact_title, display_summary, render, select, summarize
from brainsong.pipeline import load
from brainsong.provider import OfficialGLM
from brainsong.secrets import api_key
from brainsong.state import State


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    today = datetime.now(ZoneInfo('Asia/Shanghai')).date()
    cfg, _, _ = load(root)
    state = State(args.state)
    provider = OfficialGLM(api_key(), cfg['ai']['model'])
    picks, rejected = [], set()
    try:
        candidates = state.recent(today)
        while len(picks) < cfg['max_items']:
            selected = select([a for a in candidates if a.identity not in rejected], state, today, cfg)
            seen = {a.identity for a in picks}
            pending = [a for a in selected if a.identity not in seen]
            if not pending:
                break
            for item in pending[:cfg['max_items']-len(picks)]:
                if item.category == '学术' and sum(a.category == '学术' for a in picks) >= cfg.get('max_academic_items', 2):
                    rejected.add(item.identity)
                    continue
                summarize(item, provider, state, today)
                if not display_summary(item.summary, item.title):
                    rejected.add(item.identity)
                    continue
                compact_title(item, provider, state, today)
                state.save(item)
                picks.append(item)
        title, body = render(picks, today)
        output = root / 'reports' / str(today) / 'seven-preview'
        output.mkdir(parents=True, exist_ok=True)
        (output / 'brief.md').write_text(body, encoding='utf-8')
        (output / 'brief.json').write_text(json.dumps({'title': title, 'items': [a.record() for a in picks], 'sent': False}, ensure_ascii=False, indent=2), encoding='utf-8')
        (output / 'usage.json').write_text(json.dumps({'calls': provider.calls, 'usage': provider.usage}, indent=2), encoding='utf-8')
        print(json.dumps({'report': str(output / 'brief.md'), 'items': len(picks), 'sent': False, 'calls': provider.calls}, ensure_ascii=False))
    finally:
        state.close()
        provider.client.close()


if __name__ == '__main__':
    main()
