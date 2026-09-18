"""Restore a trusted default-branch daily workflow's SQLite artifact, fail closed."""
import io
import os
import sqlite3
import sys
import zipfile
from pathlib import Path

import httpx


def main():
    repo, token, branch = os.environ['GITHUB_REPOSITORY'], os.environ['GH_TOKEN'], os.environ['DEFAULT_BRANCH']
    base = f'https://api.github.com/repos/{repo}'
    with httpx.Client(headers={'Authorization': 'Bearer '+token, 'Accept': 'application/vnd.github+json'}, timeout=30, follow_redirects=False) as client:
        response = client.get(base+'/actions/artifacts', params={'name': 'brainsong-state', 'per_page': 100})
        response.raise_for_status()
        artifacts = response.json()['artifacts']
        valid = []
        for artifact in artifacts:
            if artifact['expired'] or artifact.get('workflow_run', {}).get('head_branch') != branch:
                continue
            run = client.get(base+'/actions/runs/'+str(artifact['workflow_run']['id']))
            run.raise_for_status()
            info = run.json()
            if info.get('path') == '.github/workflows/daily.yml' and info.get('event') in {'schedule','workflow_dispatch'}:
                valid.append(artifact)
        if not valid:
            if artifacts or os.getenv('INITIALIZE_STATE') != 'true':
                raise RuntimeError('History missing; only explicit first-run initialization is allowed')
            Path('state').mkdir(exist_ok=True)
            print('First-run state initialization explicitly authorized.')
            return
        artifact = max(valid, key=lambda a: a['created_at'])
        response = client.get(base+f"/actions/artifacts/{artifact['id']}/zip")
        if response.status_code != 302:
            raise RuntimeError('Invalid state download response')
        location = response.headers['location']
    # Signed artifact download receives NO GitHub authorization header.
    if not location.startswith('https://'):
        raise RuntimeError('Invalid state download URL')
    response = httpx.get(location, timeout=60, follow_redirects=True)
    response.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        name = 'brainsong.sqlite3'
        if name not in archive.namelist() or archive.getinfo(name).file_size > 100_000_000:
            raise RuntimeError('Invalid state artifact')
        folder = Path('state')
        folder.mkdir(exist_ok=True)
        path = folder/name
        path.write_bytes(archive.read(name))
    with sqlite3.connect(path) as db:
        if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise RuntimeError('Invalid SQLite state')
    print('Previous SQLite state restored and verified.')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('State restoration failed: '+type(exc).__name__)
        sys.exit(1)
