"""Transactional event evidence and deterministic state replay."""
import importlib
import json
import sqlite3


def modules():
    from .settings import MODULES
    return {name: importlib.import_module(f'.{name}', __package__) for name in MODULES}


def connect(path):
    db = sqlite3.connect(path, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA busy_timeout=5000')
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE, subsystem TEXT NOT NULL, payload TEXT NOT NULL, result TEXT NOT NULL)')
    for module in modules().values():
        db.executescript(module.SCHEMA)
    return db


def one(db, sql, args=()):
    row = db.execute(sql, args).fetchone()
    return dict(row) if row else None


def many(db, sql, args=()):
    return [dict(row) for row in db.execute(sql, args)]


def act(db, subsystem, command):
    payload = json.dumps(command, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    event_id = command['event_id']
    db.execute('BEGIN IMMEDIATE')
    try:
        prior = one(db, 'SELECT * FROM events WHERE event_id=?', (event_id,))
        if prior:
            result = json.loads(prior['result']) if prior['subsystem'] == subsystem and prior['payload'] == payload else {'decision': 'event_identity_conflict'}
        else:
            result = modules()[subsystem].handle(db, command)
            db.execute('INSERT INTO events(event_id,subsystem,payload,result) VALUES(?,?,?,?)',
                       (event_id, subsystem, payload, json.dumps(result, ensure_ascii=False, sort_keys=True)))
        db.execute('COMMIT')
        return result
    except Exception:
        db.execute('ROLLBACK')
        raise


def snapshot(db):
    names = [r['name'] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name!='events' ORDER BY name")]
    return {name: [tuple(row) for row in db.execute(f'SELECT * FROM {name} ORDER BY rowid')] for name in names}


def replay(path):
    source = connect(path)
    target = connect(':memory:')
    issues = []
    events = many(source, 'SELECT * FROM events ORDER BY seq')
    for row in events:
        actual = act(target, row['subsystem'], json.loads(row['payload']))
        if actual != json.loads(row['result']):
            issues.append({'event_id': row['event_id'], 'actual': actual})
    if snapshot(source) != snapshot(target):
        issues.append({'state': 'business tables diverged'})
    source.close()
    target.close()
    return {'decisions': len(events), 'verified': not issues, 'issues': issues}
