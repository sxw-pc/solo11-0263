import ipaddress
from .store import one,many

SCHEMA='''
CREATE TABLE IF NOT EXISTS address_leases(id TEXT PRIMARY KEY,pool_id TEXT,claim_id TEXT,ip TEXT,pod_uid TEXT,node_uid TEXT,pool_generation INTEGER,generation INTEGER,status TEXT);
CREATE TABLE IF NOT EXISTS holder_fences(id TEXT PRIMARY KEY,lease_id TEXT,generation INTEGER,at INTEGER,status TEXT);
CREATE TABLE IF NOT EXISTS address_conflicts(id TEXT PRIMARY KEY,ip TEXT,other_pod TEXT,status TEXT);
'''


def handle(db,x):
    op=x['op']
    if op=='issue':
        pool=one(db,'SELECT * FROM address_pools WHERE id=?',(x['pool'],))
        claim=one(db,'SELECT * FROM tenant_claims WHERE id=?',(x['claim'],))
        if not pool or pool['status']!='active' or not claim or claim['pool_id']!=pool['id'] or claim['status']!='active' or claim['expires']<=x['at']:
            return {'decision':'source_unavailable'}
        try: ip=ipaddress.ip_address(x['ip'])
        except ValueError:return {'decision':'invalid_ip'}
        network=ipaddress.ip_network(pool['network'])
        if ip not in network or ip in (network.network_address,network.broadcast_address) or x['pool_generation']!=pool['generation']:
            return {'decision':'address_outside_current_pool'}
        count=one(db,"SELECT COUNT(*) n FROM address_leases WHERE claim_id=? AND status IN ('bound','fenced','isolated')",(x['claim'],))['n']
        if count>=claim['capacity']:return {'decision':'claim_full'}
        occupied=one(db,"SELECT * FROM address_leases WHERE ip=? AND status IN ('bound','fenced','isolated')",(x['ip'],))
        if occupied:return {'decision':'address_occupied','holder':occupied['pod_uid']}
        db.execute('INSERT INTO address_leases VALUES(?,?,?,?,?,?,?,?,?)',(x['id'],pool['id'],claim['id'],x['ip'],x['pod_uid'],x['node_uid'],pool['generation'],1,'bound'))
        return {'decision':'bound'}
    if op=='renew':
        lease=one(db,'SELECT * FROM address_leases WHERE id=?',(x['id'],))
        if not lease or lease['status']!='bound' or lease['pod_uid']!=x['pod_uid'] or lease['node_uid']!=x['node_uid'] or lease['generation']!=x['generation']:
            return {'decision':'stale_holder'}
        return {'decision':'renewed'}
    if op=='fence':
        lease=one(db,'SELECT * FROM address_leases WHERE id=?',(x['lease'],))
        if not lease or lease['status']!='bound' or x['generation']<=lease['generation']:
            return {'decision':'fence_denied'}
        db.execute('INSERT INTO holder_fences VALUES(?,?,?,?,?)',(x['id'],x['lease'],x['generation'],x['at'],'active'))
        db.execute("UPDATE address_leases SET status='fenced',generation=? WHERE id=?",(x['generation'],x['lease']))
        return {'decision':'holder_fenced'}
    if op=='conflict':
        lease=one(db,"SELECT * FROM address_leases WHERE ip=? AND status IN ('bound','fenced')",(x['ip'],))
        if not lease or lease['pod_uid']==x['other_pod']:return {'decision':'no_conflict'}
        db.execute('INSERT INTO address_conflicts VALUES(?,?,?,?)',(x['id'],x['ip'],x['other_pod'],'isolated'))
        db.execute("UPDATE address_leases SET status='isolated' WHERE id=?",(lease['id'],))
        return {'decision':'address_isolated'}
    return {'decision':'unknown_operation'}
