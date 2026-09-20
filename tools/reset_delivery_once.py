"""Manually authorized formal launch only; no API calls and no secret output."""
import argparse
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from brainsong.state import State


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--confirm-official-launch', action='store_true')
    args = parser.parse_args()
    if not args.confirm_official_launch or os.getenv('SEND_BRIEF') != 'true':
        raise SystemExit('正式首发重置必须明确确认并启用发送')
    path = Path('state/brainsong.sqlite3')
    if not path.exists():
        raise SystemExit('历史未恢复，禁止重置')
    state = State(path)
    try:
        changed = state.reset_delivery_once(path.with_name('before-official-launch.sqlite3'),
                                           datetime.now(ZoneInfo('Asia/Shanghai')).date())
        print('正式首发历史已备份并重置' if changed else '正式首发已重置过，本次保留发送历史')
    finally:
        state.close()


if __name__ == '__main__':
    main()
