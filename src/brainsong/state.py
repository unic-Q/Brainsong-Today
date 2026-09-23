import json
import sqlite3
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
        CREATE TABLE IF NOT EXISTS delivered(alias TEXT PRIMARY KEY, day TEXT, status TEXT);
        CREATE TABLE IF NOT EXISTS policy_delivered(alias TEXT PRIMARY KEY, day TEXT, status TEXT);
        CREATE TABLE IF NOT EXISTS deleted_candidates(alias TEXT PRIMARY KEY);
        CREATE INDEX IF NOT EXISTS delivered_date ON delivered(day);
        CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS errors(id INTEGER PRIMARY KEY, day TEXT, stage TEXT, detail TEXT);
        CREATE INDEX IF NOT EXISTS errors_date ON errors(day);
        """)
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
                value['published'] = value['published'] or previous.get('published', '')
                value['body'] = value['body'] or previous.get('body', '')
            self.db.execute('''INSERT INTO candidate_inputs VALUES (?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen, payload=excluded.payload''',
                (item.identity, str(today), str(today), None, json.dumps(value, ensure_ascii=False)))
        self.db.commit()

    def candidates(self):
        return [a for r in self.db.execute('SELECT payload FROM candidate_inputs')
                if not self.deleted(a := Article(**json.loads(r[0])))]

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
                                        [(prefix + key,) for prefix in ('ai:', 'summary-v2:', 'summary-v3:', 'summary-v4:', 'summary-v5:')])

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
            count_row = prior.execute("SELECT value FROM kv WHERE key='counts'").fetchone()
        current_delivery = {row[0] for row in self.db.execute('SELECT alias FROM delivered')}
        current_policy = {row[0] for row in self.db.execute('SELECT alias FROM policy_delivered')}
        current_reports = {row[0] for row in self.db.execute("SELECT key FROM kv WHERE key LIKE 'report:%'")}
        added_delivery = current_delivery - prior_delivery
        added_policy = current_policy - prior_policy
        added_reports = current_reports - set(prior_reports)
        with self.db:
            self.db.executemany('DELETE FROM delivered WHERE alias=?', [(x,) for x in added_delivery])
            self.db.executemany('DELETE FROM policy_delivered WHERE alias=?', [(x,) for x in added_policy])
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

    def prune(self, today):
        cutoff = (today-timedelta(days=30)).isoformat()
        expired = [Article(**json.loads(payload)) for first_seen, payload in
                   self.db.execute('SELECT first_seen,payload FROM candidate_inputs')
                   if (json.loads(payload).get('published') or first_seen)[:10] <= cutoff]
        expired.extend(Article(**json.loads(payload)) for (payload,) in self.db.execute(
            "SELECT payload FROM articles WHERE published!='' AND substr(published,1,10)<=?", (cutoff,)))
        self.delete_candidates(expired)
        self.db.execute("DELETE FROM errors WHERE day<?", ((today-timedelta(days=90)).isoformat(),))
        self.db.execute("DELETE FROM delivered WHERE day<? AND status!='pending'", ((today-timedelta(days=365)).isoformat(),))
        cutoff = (today-timedelta(days=30)).isoformat()
        stale = []
        for key, value in self.db.execute("SELECT key,value FROM kv WHERE key LIKE 'ai:%' OR key LIKE 'summary-v2:%' OR key LIKE 'summary-v3:%' OR key LIKE 'summary-v4:%' OR key LIKE 'summary-v5:%' OR key LIKE 'headline-v1:%' OR key LIKE 'dedup:%'"):
            if json.loads(value).get("day", "") < cutoff:
                stale.append((key,))
        self.db.executemany("DELETE FROM kv WHERE key=?", stale)
        self.db.execute("DELETE FROM kv WHERE key LIKE 'report:%' AND substr(key,8,10)<? AND value!='\"pending\"'",
                        ((today-timedelta(days=90)).isoformat(),))
        self.db.execute("PRAGMA optimize")
        self.db.commit()

    def close(self):
        self.db.close()
