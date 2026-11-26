"""Transfer unused claim capacity without touching existing leases."""
from .store import one

SCHEMA = '''
CREATE TABLE IF NOT EXISTS capacity_transfers(
    id TEXT PRIMARY KEY, donor TEXT NOT NULL, receiver TEXT NOT NULL,
    pool_generation INTEGER NOT NULL, units INTEGER NOT NULL, at INTEGER NOT NULL
);
'''


def handle(db, command):
    if command['op'] != 'move':
        return {'decision': 'unknown_operation'}
    units = command['units']
    at = command['at']
    if type(units) is not int or units < 1 or type(at) is not int or command['donor'] == command['receiver']:
        return {'decision': 'invalid_capacity_move'}
    if one(db, 'SELECT id FROM capacity_transfers WHERE id=?', (command['id'],)):
        return {'decision': 'capacity_move_exists'}
    donor = one(db, 'SELECT * FROM tenant_claims WHERE id=?', (command['donor'],))
    receiver = one(db, 'SELECT * FROM tenant_claims WHERE id=?', (command['receiver'],))
    if not donor or not receiver or donor['pool_id'] != receiver['pool_id']:
        return {'decision': 'incompatible_capacity_claims'}
    pool = one(db, 'SELECT * FROM address_pools WHERE id=?', (donor['pool_id'],))
    if not pool or pool['status'] != 'active' or any(
        claim['tenant'] != pool['tenant'] or claim['pool_generation'] != pool['generation']
        or claim['status'] != 'active' or claim['expires'] <= at
        for claim in (donor, receiver)
    ):
        return {'decision': 'capacity_source_unavailable'}
    held = one(db, "SELECT COUNT(*) n FROM address_leases WHERE claim_id=? AND status IN ('bound','fenced','isolated')", (donor['id'],))['n']
    if donor['capacity'] - units < max(held, 1):
        return {'decision': 'capacity_needed_by_donor'}
    donor_capacity = donor['capacity'] - units
    receiver_capacity = receiver['capacity'] + units
    db.execute('UPDATE tenant_claims SET capacity=? WHERE id=?', (donor_capacity, donor['id']))
    db.execute('UPDATE tenant_claims SET capacity=? WHERE id=?', (receiver_capacity, receiver['id']))
    db.execute('INSERT INTO capacity_transfers VALUES(?,?,?,?,?,?)', (
        command['id'], donor['id'], receiver['id'], pool['generation'], units, at,
    ))
    return {'decision': 'capacity_moved', 'donor_capacity': donor_capacity, 'receiver_capacity': receiver_capacity}
