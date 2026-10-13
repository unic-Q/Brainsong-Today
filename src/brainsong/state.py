import json
import sqlite3
import csv
from datetime import date, timedelta
from pathlib import Path

from .model import Article


class State:
    def __init__(self, path):
        self.progress = None
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript("""
        PRAGMA journal_mode=DELETE;
        CREATE TABLE IF NOT EXISTS articles(id TEXT PRIMARY KEY, published TEXT, payload TEXT);
        CREATE INDEX IF NOT EXISTS article_date ON articles(published);
        CREATE TABLE IF NOT EXISTS candidate_inputs(
            id TEXT PRIMARY KEY, first_seen TEXT, last_seen TEXT, released TEXT, payload TEXT);
        CREATE TABLE IF NOT EXISTS observed_candidates(id TEXT PRIMARY KEY, first_seen TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS delivered(alias TEXT PRIMARY KEY, day TEXT, status TEXT);
        CREATE TABLE IF NOT EXISTS policy_delivered(alias TEXT PRIMARY KEY, day TEXT, status TEXT);
        CREATE TABLE IF NOT EXISTS sent_events(id TEXT PRIMARY KEY, day TEXT, category TEXT, payload TEXT);
        CREATE INDEX IF NOT EXISTS sent_events_day ON sent_events(day);
        CREATE TABLE IF NOT EXISTS source_authority(
            publisher_key TEXT PRIMARY KEY, name TEXT, url TEXT, authority REAL,
            recognition REAL, basis TEXT, first_seen TEXT);
        CREATE TABLE IF NOT EXISTS deleted_candidates(alias TEXT PRIMARY KEY);
        CREATE INDEX IF NOT EXISTS delivered_date ON delivered(day);
        CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS errors(id INTEGER PRIMARY KEY, day TEXT, stage TEXT, detail TEXT);
        CREATE INDEX IF NOT EXISTS errors_date ON errors(day);
        """)
        # Preserve first discovery across automatic candidate expiry. This is a
        # lightweight identity ledger, not a copy of article text.
        self.db.execute("""INSERT OR IGNORE INTO observed_candidates(id, first_seen)
            SELECT id, first_seen FROM candidate_inputs WHERE first_seen IS NOT NULL""")
        self.db.commit()
        # Migrate still-retained sent articles when upgrading an existing DB.
        for (payload,) in self.db.execute('SELECT payload FROM articles'):
            item = Article(**json.loads(payload))
            if self.sent(item):
                row = self.db.execute('SELECT day FROM delivered WHERE alias=?',
                                      (item.aliases()[0],)).fetchone()
                if row:
                    self.db.execute('INSERT OR IGNORE INTO sent_events VALUES (?,?,?,?)',
                                    (item.identity, row[0], item.category,
                                     json.dumps(item.record(), ensure_ascii=False)))
        self.db.commit()
        check = self.db.execute("PRAGMA quick_check").fetchone()[0]
        if check != "ok":
            raise RuntimeError("历史数据库损坏，停止推送")

    def preview_without_delivery_history(self):
        """Use a disposable snapshot so previews neither read nor change delivery history."""
        preview = State(":memory:")
        self.db.backup(preview.db)
        with preview.db:
            preview.db.execute('DELETE FROM delivered')
            preview.db.execute('DELETE FROM policy_delivered')
            preview.db.execute('DELETE FROM sent_events')
        return preview

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (key, json.dumps(value, ensure_ascii=False)))
        self.db.commit()

    def save(self, item):
        if self.deleted(item):
            return
        self.db.execute("INSERT OR REPLACE INTO articles VALUES (?,?,?)",
                        (item.identity, item.published, json.dumps(item.record(), ensure_ascii=False)))
        self.db.commit()

    def cached(self, item):
        row = self.db.execute("SELECT payload FROM articles WHERE id=?", (item.identity,)).fetchone()
        return json.loads(row[0]) if row else None

    def retain_candidates(self, items, today):
        """Checkpoint source evidence BEFORE filtering/AI; never replace with AI summaries."""
        for item in items:
            if self.deleted(item):
                continue
            if not item.published:
                self.db.execute("INSERT OR IGNORE INTO observed_candidates VALUES (?,?)",
                                (item.identity, str(today)))
            value = item.record()
            value['summary'] = value['source_summary']
            value['summary_kind'] = 'source'
            value['summary_version'] = ''
            value['body'] = ''
            old = self.db.execute('SELECT payload FROM candidate_inputs WHERE id=?', (item.identity,)).fetchone()
            if old:
                previous = json.loads(old[0])
                prior_source = previous.get('source_summary', '') or (previous.get('summary', '') if previous.get('summary_kind') not in {'ai', 'failed', 'formatted'} else '')
                if len(prior_source) > len(value['summary']):
                    value['summary'] = prior_source
                value['source_summary'] = value['summary']
                value['source_excerpt'] = value['source_excerpt'] or previous.get('source_excerpt', '') or previous.get('body', '')[:6000]
                if not value['published'] and previous.get('published'):
                    value['published'] = previous['published']
                    value['date_evidence'] = previous.get('date_evidence', '')
                value['first_reported'] = value['first_reported'] or previous.get('first_reported', '')
                value['body'] = value['body'] or previous.get('body', '')
            self.db.execute('''INSERT INTO candidate_inputs VALUES (?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen, payload=excluded.payload''',
                (item.identity, str(today), str(today), None, json.dumps(value, ensure_ascii=False)))
        self.db.commit()

    def candidates(self):
        return [a for r in self.db.execute('SELECT payload FROM candidate_inputs')
                if not self.deleted(a := Article(**json.loads(r[0])))]

    def ever_seen(self, item):
        """A fresh listing is only one absent from all persisted candidate/history tables."""
        if self.deleted(item) or self.sent(item):
            return True
        return any(self.db.execute(f'SELECT 1 FROM {table} WHERE id=?', (item.identity,)).fetchone()
                   for table in ('observed_candidates', 'candidate_inputs', 'articles'))

    def first_seen(self, item):
        row = self.db.execute('SELECT first_seen FROM observed_candidates WHERE id=?',
                              (item.identity,)).fetchone()
        return row[0] if row else ''

    def deleted(self, item):
        return any(self.db.execute('SELECT 1 FROM deleted_candidates WHERE alias=?', (k,)).fetchone()
                   for k in item.aliases())

    def delete_candidates(self, items):
        """Erase candidate material, keeping only permanent non-revival fingerprints."""
        items = list(items)
        if not items:
            return
        with self.db:
            self.db.executemany('INSERT OR IGNORE INTO deleted_candidates VALUES (?)',
                                [(k,) for a in items for k in a.aliases()])
            for table in ('candidate_inputs', 'articles'):
                ids = [key for key, payload in self.db.execute(f'SELECT id,payload FROM {table}')
                       if self.deleted(Article(**json.loads(payload)))]
                self.db.executemany(f'DELETE FROM {table} WHERE id=?', [(key,) for key in ids])
                for key in ids:
                    self.db.executemany('DELETE FROM kv WHERE key=?',
                                        [(prefix + key,) for prefix in ('ai:', 'summary-v2:', 'summary-v3:', 'summary-v4:', 'summary-v5:', 'summary-v6:')])

    def release_candidates(self, today):
        """Record successful delivery; expiry remains based on publication/first seen."""
        self.db.execute('UPDATE candidate_inputs SET released=? WHERE released IS NULL', (str(today),))
        self.db.commit()

    def recent(self, today, days=30):
        return [a for r in self.db.execute(
            "SELECT payload FROM articles WHERE published>=? OR json_extract(payload,'$.category')='政策'", ((today-timedelta(days=days)).isoformat(),))
                if not self.deleted(a := Article(**json.loads(r[0])))]

    def sent(self, item):
        return any(self.db.execute("SELECT 1 FROM delivered WHERE alias=? AND status IN ('sent','pending') UNION ALL SELECT 1 FROM policy_delivered WHERE alias=? AND status IN ('sent','pending')", (k, k)).fetchone()
                   for k in item.aliases())

    def delivery_due(self, today, interval):
        days = []
        for key, value in self.db.execute("SELECT key,value FROM kv WHERE key LIKE 'daily-delivery:%'"):
            if json.loads(value) == 'sent':
                try:
                    days.append(date.fromisoformat(key.removeprefix('daily-delivery:')))
                except ValueError:
                    pass
        return not days or (today - max(days)).days >= interval

    def mark(self, items, today, status):
        self.db.executemany("INSERT OR REPLACE INTO delivered VALUES (?,?,?)",
                            [(k, today.isoformat(), status) for a in items for k in a.aliases()])
        self.db.executemany("INSERT OR REPLACE INTO policy_delivered VALUES (?,?,?)",
                            [(k, today.isoformat(), status) for a in items if a.category == '政策' for k in a.aliases()])
        self.db.commit()

    def record_sent_events(self, items, today):
        with self.db:
            self.db.executemany('INSERT OR REPLACE INTO sent_events VALUES (?,?,?,?)',
                                [(a.identity, today.isoformat(), a.category,
                                  json.dumps(a.record(), ensure_ascii=False)) for a in items])

    def recent_sent_events(self, today, days=60):
        cutoff = (today-timedelta(days=days)).isoformat()
        return [Article(**json.loads(payload)) for (payload,) in self.db.execute(
            "SELECT payload FROM sent_events WHERE day>=? OR category='政策' ORDER BY day DESC", (cutoff,))]

    def source_authority(self, publisher_key):
        row = self.db.execute('SELECT authority,recognition,basis FROM source_authority '
                              'WHERE publisher_key=?', (publisher_key,)).fetchone()
        return dict(zip(('authority','recognition','basis'), row)) if row else None

    def record_source_authority(self, key, name, url, authority, recognition, basis, today):
        self.db.execute('INSERT OR IGNORE INTO source_authority VALUES (?,?,?,?,?,?,?)',
                        (key, name, url, authority, recognition, basis, today.isoformat()))
        self.db.commit()

    def update_source_authority(self, key, authority, recognition, basis):
        self.db.execute('UPDATE source_authority SET authority=?,recognition=?,basis=? '
                        'WHERE publisher_key=?', (authority, recognition, basis, key))
        self.db.commit()

    def export_source_authority(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('w', encoding='utf-8-sig', newline='') as handle:
            writer = csv.writer(handle)
            writer.writerow(['信源标识','名称','示例网址','权威性','知名度','评分依据','首次收录'])
            writer.writerows(self.db.execute('SELECT publisher_key,name,url,authority,recognition,basis,first_seen '
                                            'FROM source_authority ORDER BY authority DESC,recognition DESC'))

    def reset_delivery_once(self, backup_path, today):
        """Explicit formal-launch migration, not a normal startup/reset operation."""
        marker = 'deployment:official-history-reset'
        if self.get(marker):
            return False
        if self.db.execute("SELECT 1 FROM delivered WHERE status='pending' UNION ALL SELECT 1 FROM policy_delivered WHERE status='pending'").fetchone():
            raise RuntimeError('存在未确认发送，先核实后才能重置')
        backup = Path(backup_path)
        if backup.exists():
            raise RuntimeError('备份已存在，停止以免覆盖')
        backup.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(backup) as target:
            self.db.backup(target)
        with self.db:
            self.db.execute('DELETE FROM delivered')
            self.db.execute('DELETE FROM policy_delivered')
            self.db.execute('DELETE FROM sent_events')
            self.db.execute("DELETE FROM kv WHERE key LIKE 'report:%' OR key='counts'")
            self.db.execute('INSERT INTO kv VALUES (?,?)',
                            (marker,json.dumps({'day':str(today)},ensure_ascii=False)))
        return True

    def rollback_delivery_to(self, prior_path, backup_path, marker):
        """Remove only delivery state added after a trusted earlier snapshot."""
        marker_key = 'delivery-rollback:' + str(marker)
        if self.get(marker_key):
            return {'delivered': 0, 'policy': 0, 'reports': 0}
        if self.db.execute("SELECT 1 FROM delivered WHERE status='pending' UNION ALL SELECT 1 FROM policy_delivered WHERE status='pending'").fetchone():
            raise RuntimeError('存在未确认发送，不能回滚')
        prior_path, backup = Path(prior_path), Path(backup_path)
        if not prior_path.is_file() or backup.exists():
            raise RuntimeError('回滚基线或备份路径无效')
        backup.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(backup) as target:
            self.db.backup(target)
        with sqlite3.connect(prior_path) as prior:
            if prior.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise RuntimeError('回滚基线数据库损坏')
            prior_delivery = {row[0] for row in prior.execute('SELECT alias FROM delivered')}
            prior_policy = {row[0] for row in prior.execute('SELECT alias FROM policy_delivered')}
            prior_reports = {row[0]: row[1] for row in prior.execute("SELECT key,value FROM kv WHERE key LIKE 'report:%'")}
            try:
                prior_events = {row[0] for row in prior.execute('SELECT id FROM sent_events')}
            except sqlite3.OperationalError:
                prior_events = set()
            count_row = prior.execute("SELECT value FROM kv WHERE key='counts'").fetchone()
        current_delivery = {row[0] for row in self.db.execute('SELECT alias FROM delivered')}
        current_policy = {row[0] for row in self.db.execute('SELECT alias FROM policy_delivered')}
        current_reports = {row[0] for row in self.db.execute("SELECT key FROM kv WHERE key LIKE 'report:%'")}
        added_events = {row[0] for row in self.db.execute('SELECT id FROM sent_events')} - prior_events
        added_delivery = current_delivery - prior_delivery
        added_policy = current_policy - prior_policy
        added_reports = current_reports - set(prior_reports)
        with self.db:
            self.db.executemany('DELETE FROM delivered WHERE alias=?', [(x,) for x in added_delivery])
            self.db.executemany('DELETE FROM policy_delivered WHERE alias=?', [(x,) for x in added_policy])
            self.db.executemany('DELETE FROM sent_events WHERE id=?', [(x,) for x in added_events])
            self.db.executemany('DELETE FROM kv WHERE key=?', [(x,) for x in added_reports])
            if count_row:
                self.db.execute("INSERT OR REPLACE INTO kv VALUES ('counts',?)", count_row)
            else:
                self.db.execute("DELETE FROM kv WHERE key='counts'")
            self.db.execute('INSERT INTO kv VALUES (?,?)',
                            (marker_key, json.dumps({'delivered': len(added_delivery),
                                                     'policy': len(added_policy),
                                                     'reports': len(added_reports)}, ensure_ascii=False)))
        return {'delivered': len(added_delivery), 'policy': len(added_policy), 'reports': len(added_reports)}

    def error(self, today, stage, exc):
        # Never store HTTP bodies, headers, raw exception messages, keys or webhook URLs.
        detail = type(exc).__name__ if isinstance(exc, Exception) else str(exc)[:100]
        from .provider import QualityError
        if isinstance(exc, QualityError):
            detail = exc.safe_detail[:300]
        if hasattr(exc, "response") and hasattr(exc.response, "status_code"):
            detail += " HTTP " + str(exc.response.status_code)
        self.db.execute("INSERT INTO errors(day,stage,detail) VALUES (?,?,?)",
                        (today.isoformat(), stage[:100], detail))
        self.db.commit()
        if self.progress:
            self.progress('error ' + stage.split(':', 1)[0] + ' ' + detail.split(' ', 1)[0])

    def prune(self, today, candidate_days=30, *, drop_undated=False, use_first_reported=False,
              event_days=60):
        """Discard stale candidate material without removing delivery fingerprints."""
        def expired_candidate(item, first_seen=''):
            if not item.published or item.date_evidence == 'recent_first_seen':
                value = first_seen or self.first_seen(item) or item.first_reported
            else:
                value = item.first_reported if use_first_reported and item.first_reported else item.published
            try:
                age = (today-date.fromisoformat(value[:10])).days
            except ValueError:
                return True
            return not 0 <= age < candidate_days

        expired = [item for first_seen, payload in
                   self.db.execute('SELECT first_seen,payload FROM candidate_inputs')
                   if expired_candidate(item := Article(**json.loads(payload)), first_seen)]
        expired.extend(item for (payload,) in self.db.execute('SELECT payload FROM articles')
                       if (item := Article(**json.loads(payload))).published or drop_undated
                       if expired_candidate(item))
        # Explicit deletion and dated-out-of-window records keep the old
        # permanent rejection policy. Missing dates may be resolved later, so
        # their automatic expiry must never become a permanent rejection.
        self.delete_candidates(item for item in expired
                               if item.published and item.date_evidence != 'recent_first_seen')
        undated_ids = {item.identity for item in expired
                       if not item.published or item.date_evidence == 'recent_first_seen'}
        with self.db:
            for table in ('candidate_inputs', 'articles'):
                self.db.executemany(f'DELETE FROM {table} WHERE id=?',
                                    [(identity,) for identity in undated_ids])
        self.db.execute("DELETE FROM errors WHERE day<?", ((today-timedelta(days=90)).isoformat(),))
        self.db.execute("DELETE FROM delivered WHERE day<? AND status!='pending'", ((today-timedelta(days=365)).isoformat(),))
        self.db.execute("DELETE FROM sent_events WHERE day<? AND category!='政策'", ((today-timedelta(days=event_days)).isoformat(),))
        cutoff = (today-timedelta(days=30)).isoformat()
        stale = []
        for key, value in self.db.execute("SELECT key,value FROM kv WHERE key LIKE 'ai:%' OR key LIKE 'summary-v2:%' OR key LIKE 'summary-v3:%' OR key LIKE 'summary-v4:%' OR key LIKE 'summary-v5:%' OR key LIKE 'summary-v6:%' OR key LIKE 'headline-v1:%' OR key LIKE 'dedup:%' OR key LIKE 'event-v1:%' OR key LIKE 'launch-v1:%' OR key LIKE 'product-page-v1:%'"):
            if json.loads(value).get("day", "") < cutoff:
                stale.append((key,))
        self.db.executemany("DELETE FROM kv WHERE key=?", stale)
        self.db.execute("DELETE FROM kv WHERE key LIKE 'report:%' AND substr(key,8,10)<? AND value!='\"pending\"'",
                        ((today-timedelta(days=90)).isoformat(),))
        self.db.execute("PRAGMA optimize")
        self.db.commit()

    def close(self):
        self.db.close()
