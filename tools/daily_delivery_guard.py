"""Skip later retry slots after one confirmed Beijing-date delivery."""
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from brainsong.state import State


def main():
    should_run = True
    if os.getenv('DAILY_DELIVERY') == 'true':
        today = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
        state = State(Path('state') / 'brainsong.sqlite3')
        try:
            should_run = state.get('daily-delivery:' + today) != 'sent'
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
