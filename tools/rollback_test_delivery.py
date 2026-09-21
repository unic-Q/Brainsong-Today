"""Precisely remove delivery fingerprints introduced by one test workflow run."""
import io
import os
import sys
import tempfile
import zipfile
from pathlib import Path

import httpx

from brainsong.state import State


def download(client, base, artifact):
    response = client.get(base + f"/actions/artifacts/{artifact['id']}/zip")
    if response.status_code != 302 or not response.headers.get('location', '').startswith('https://'):
        raise RuntimeError('Invalid state artifact response')
    response = httpx.get(response.headers['location'], timeout=60, follow_redirects=True)
    response.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        if 'brainsong.sqlite3' not in archive.namelist() or archive.getinfo('brainsong.sqlite3').file_size > 100_000_000:
            raise RuntimeError('Invalid state artifact')
        return archive.read('brainsong.sqlite3')


def main(run_id):
    if not run_id.isdigit():
        raise RuntimeError('运行ID格式错误')
    repo, token, branch = os.environ['GITHUB_REPOSITORY'], os.environ['GH_TOKEN'], os.environ['DEFAULT_BRANCH']
    base = f'https://api.github.com/repos/{repo}'
    headers = {'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json'}
    with httpx.Client(headers=headers, timeout=30, follow_redirects=False) as client:
        response = client.get(base + '/actions/artifacts', params={'name': 'brainsong-state', 'per_page': 100})
        response.raise_for_status()
        artifacts = [a for a in response.json()['artifacts'] if not a['expired']
                     and a.get('workflow_run', {}).get('head_branch') == branch]
        target = next((a for a in artifacts if str(a['workflow_run']['id']) == run_id), None)
        if target is None:
            raise RuntimeError('找不到指定测试运行的状态')
        newest = max(artifacts, key=lambda a: a['created_at'])
        if newest['id'] != target['id']:
            raise RuntimeError('指定测试运行不是当前状态，停止以免删除后续历史')
        earlier = [a for a in artifacts if a['created_at'] < target['created_at']]
        if not earlier:
            raise RuntimeError('找不到测试前的状态基线')
        prior = max(earlier, key=lambda a: a['created_at'])
        payload = download(client, base, prior)
    with tempfile.TemporaryDirectory() as folder:
        prior_path = Path(folder) / 'prior.sqlite3'
        prior_path.write_bytes(payload)
        state = State('state/brainsong.sqlite3')
        try:
            result = state.rollback_delivery_to(
                prior_path, Path('state') / f'before-test-rollback-{run_id}.sqlite3', run_id)
        finally:
            state.close()
    print('Test delivery rolled back:', ', '.join(f'{k}={v}' for k, v in result.items()))


if __name__ == '__main__':
    try:
        main(sys.argv[1] if len(sys.argv) == 2 else '')
    except Exception as exc:
        print('Test delivery rollback failed: ' + type(exc).__name__)
        sys.exit(1)
