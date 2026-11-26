import ipaddress
from .store import one,many

SCHEMA='''
CREATE TABLE IF NOT EXISTS address_pools(id TEXT PRIMARY KEY,network TEXT,tenant TEXT,generation INTEGER,domain TEXT,status TEXT);
CREATE TABLE IF NOT EXISTS tenant_claims(id TEXT PRIMARY KEY,pool_id TEXT,tenant TEXT,pool_generation INTEGER,capacity INTEGER,expires INTEGER,status TEXT);
CREATE TABLE IF NOT EXISTS pool_changes(id TEXT PRIMARY KEY,pool_id TEXT,new_tenant TEXT,new_generation INTEGER,status TEXT);
'''


def handle(db,x):
    op=x['op']
    if op=='publish':
        try: network=ipaddress.ip_network(x['network'],strict=True)
        except ValueError:return {'decision':'invalid_cidr'}
        if network.version!=4:return {'decision':'ipv6_not_supported'}
        old=one(db,'SELECT * FROM address_pools WHERE id=?',(x['id'],))
        if old and x['generation']<=old['generation']:return {'decision':'stale_pool'}
        for other in many(db,"SELECT * FROM address_pools WHERE status='active' AND id!=?",(x['id'],)):
            if network.overlaps(ipaddress.ip_network(other['network'])):return {'decision':'overlapping_pool'}
        if old and one(db,"SELECT 1 FROM address_leases WHERE pool_id=? AND status IN ('bound','fenced','isolated')",(x['id'],)):
            return {'decision':'pool_has_live_leases'}
        db.execute('INSERT INTO address_pools VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET network=excluded.network,tenant=excluded.tenant,generation=excluded.generation,domain=excluded.domain,status=excluded.status',(x['id'],str(network),x['tenant'],x['generation'],x['domain'],'active'))
        return {'decision':'pool_active'}
    if op=='claim':
        pool=one(db,'SELECT * FROM address_pools WHERE id=?',(x['pool'],))
        if not pool or pool['status']!='active' or pool['tenant']!=x['tenant'] or x['capacity']<1 or x['expires']<=x['at']:
            return {'decision':'claim_denied'}
        usable=ipaddress.ip_network(pool['network']).num_addresses-2
        total=one(db,"SELECT COALESCE(SUM(capacity),0) n FROM tenant_claims WHERE pool_id=? AND status='active' AND expires>?",(x['pool'],x['at']))['n']
        if total+x['capacity']>usable:return {'decision':'pool_overcommitted'}
        db.execute('INSERT INTO tenant_claims VALUES(?,?,?,?,?,?,?)',(x['id'],x['pool'],x['tenant'],pool['generation'],x['capacity'],x['expires'],'active'))
        return {'decision':'claim_active'}
    if op=='expire_claim':
        claim=one(db,'SELECT * FROM tenant_claims WHERE id=?',(x['id'],))
        if not claim or claim['expires']>=x['at']:return {'decision':'claim_still_valid'}
        db.execute("UPDATE tenant_claims SET status='expired' WHERE id=?",(x['id'],))
        return {'decision':'new_allocations_stopped'}
    if op=='transfer':
        pool=one(db,'SELECT * FROM address_pools WHERE id=?',(x['pool'],))
        if not pool or x['generation']<=pool['generation']:return {'decision':'stale_transfer'}
        outstanding=one(db,"SELECT COUNT(*) n FROM address_leases WHERE pool_id=? AND status IN ('bound','fenced','isolated')",(x['pool'],))['n']
        if outstanding:return {'decision':'transfer_blocked_by_leases','count':outstanding}
        db.execute('INSERT INTO pool_changes VALUES(?,?,?,?,?)',(x['id'],x['pool'],x['tenant'],x['generation'],'committed'))
        db.execute('UPDATE address_pools SET tenant=?,generation=? WHERE id=?',(x['tenant'],x['generation'],x['pool']))
        return {'decision':'transferred'}
    return {'decision':'unknown_operation'}
