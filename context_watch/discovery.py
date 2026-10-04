"""Find rollout files and optionally read chat titles from the local state DB."""
import json
from pathlib import Path
import sqlite3


def discover(home, include_archived=False):
    home = Path(home).expanduser()
    titles = {}
    for database in sorted(home.glob('state_*.sqlite'), reverse=True):
        try:
            with sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=0.2) as conn:
                conn.execute('PRAGMA query_only=ON')
                for identity, title in conn.execute('SELECT id, title FROM threads'):
                    titles[identity] = title
            break
        except sqlite3.Error:
            continue
    paths = list((home / 'sessions').rglob('*.jsonl'))
    if include_archived:
        paths.extend((home / 'archived_sessions').rglob('*.jsonl'))
    sessions = []
    for path in paths:
        try:
            stat = path.stat()
            with path.open('rb') as stream:
                raw = stream.readline(2 * 1024 * 1024)
            try:
                record = json.loads(raw)
                payload = record.get('payload', {}) if isinstance(record, dict) else {}
                if not isinstance(payload, dict):
                    payload = {}
            except (ValueError, UnicodeError):
                payload = {}
            identity = payload.get('id') or payload.get('session_id') or path.stem
            if not isinstance(identity, str):
                identity = path.stem
            sessions.append({'id': identity, 'title': titles.get(identity, ''),
                'path': str(path.resolve()), 'cwd': payload.get('cwd', ''),
                'mtime': stat.st_mtime, 'size': stat.st_size,
                'archived': 'archived_sessions' in path.parts})
        except OSError:
            continue
    return sorted(sessions, key=lambda row: row['mtime'], reverse=True)


def choose(sessions, identity=None):
    if not sessions:
        raise ValueError('Логи сессий не найдены. Укажите --home или --file.')
    if identity:
        exact = [s for s in sessions if s['id'] == identity]
        matches = exact or [s for s in sessions if str(s['id']).startswith(identity)]
        if len(matches) != 1:
            raise ValueError('Сессия не найдена или префикс неоднозначен: ' + identity)
        return matches[0]
    return sessions[0]
