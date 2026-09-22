"""Skip later retry slots after one confirmed Beijing-date delivery."""
import os
import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from brainsong.state import State


def delivery_due(state, today, interval):
    days = []
    for key, value in state.db.execute("SELECT key,value FROM kv WHERE key LIKE 'daily-delivery:%'"):
        if json.loads(value) == 'sent':
            try:
                days.append(date.fromisoformat(key.removeprefix('daily-delivery:')))
            except ValueError:
                pass
    return not days or (today - max(days)).days >= interval


def main():
    should_run = True
    if os.getenv('DAILY_DELIVERY') == 'true':
        today = datetime.now(ZoneInfo('Asia/Shanghai')).date()
        config = yaml.safe_load(Path('config/brainsong.yaml').read_text(encoding='utf-8'))
        profiles = config['operation_profiles']
        interval = profiles['presets'][profiles['active']]['delivery_interval_days']
        state = State(Path('state') / 'brainsong.sqlite3')
        try:
            should_run = delivery_due(state, today, interval)
        finally:
            state.close()
    output = os.getenv('GITHUB_OUTPUT')
    if not output:
        raise RuntimeError('GITHUB_OUTPUT missing')
    with open(output, 'a', encoding='utf-8') as handle:
        handle.write('should_run=' + ('true' if should_run else 'false') + '\n')
    print('Daily delivery required.' if should_run else 'Daily delivery already confirmed; retry skipped.')


if __name__ == '__main__':
    main()
