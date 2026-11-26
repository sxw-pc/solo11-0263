"""Serve an offline service name for a bounded, active address lease."""
import re

from .store import one

SCHEMA = '''
CREATE TABLE IF NOT EXISTS service_names(
    id TEXT PRIMARY KEY, name TEXT NOT NULL, lease_id TEXT NOT NULL,
    lease_generation INTEGER NOT NULL, address TEXT NOT NULL,
    owner TEXT NOT NULL, start_at INTEGER NOT NULL, expires INTEGER NOT NULL
);
'''
NAME = re.compile(r'[a-z][a-z0-9-]{0,62}')


def handle(db, command):
    operation = command['op']
    if operation not in ('publish', 'lookup'):
        return {'decision': 'unknown_operation'}
    name = command['name']
    at = command['at']
    if not isinstance(name, str) or not NAME.fullmatch(name) or type(at) is not int:
        return {'decision': 'invalid_name_query'}
    record = one(db, 'SELECT * FROM service_names WHERE name=? AND start_at<=? AND expires>?', (name, at, at))
    if operation == 'publish':
        if type(command['expires']) is not int or command['expires'] <= at:
            return {'decision': 'invalid_name_lifetime'}
        lease = one(db, 'SELECT * FROM address_leases WHERE id=?', (command['lease'],))
        if not lease or lease['status'] != 'bound' or lease['pod_uid'] != command['owner']:
            return {'decision': 'name_lease_unavailable'}
        if one(db, 'SELECT id FROM service_names WHERE id=? OR (name=? AND start_at<? AND expires>?)',
               (command['id'], name, command['expires'], at)):
            return {'decision': 'name_reserved'}
        db.execute('INSERT INTO service_names VALUES(?,?,?,?,?,?,?,?)', (
            command['id'], name, lease['id'], lease['generation'], lease['ip'], lease['pod_uid'], at, command['expires'],
        ))
        return {'decision': 'name_published'}
    if not record:
        return {'decision': 'name_unavailable'}
    lease = one(db, 'SELECT * FROM address_leases WHERE id=?', (record['lease_id'],))
    if not lease or lease['status'] != 'bound' or lease['generation'] != record['lease_generation']:
        return {'decision': 'name_source_unavailable'}
    return {'decision': 'name_resolved', 'address': record['address'], 'owner': record['owner'], 'expires': record['expires']}
