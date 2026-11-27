"""Recovery permits that restore wrongly isolated holders to service."""
import ipaddress
from .store import one

SCHEMA='''
CREATE TABLE IF NOT EXISTS recovery_permits(id TEXT PRIMARY KEY,conflict_id TEXT,lease_id TEXT,lease_generation INTEGER,holder_pod TEXT,holder_node TEXT,mode TEXT,ip TEXT,receipt TEXT,at INTEGER,status TEXT);
'''


def handle(db,x):
    op=x['op']
    if op not in ('restore','replace'):return {'decision':'unknown_operation'}
    conflict=one(db,'SELECT * FROM address_conflicts WHERE id=?',(x['conflict'],))
    lease=one(db,'SELECT * FROM address_leases WHERE id=?',(x['lease'],))
    if not conflict or not lease or conflict['ip']!=lease['ip']:return {'decision':'unknown_conflict'}
    if not x.get('independent_receipt'):return {'decision':'recovery_denied'}
    if lease['status']!='isolated':return {'decision':'lease_not_isolated'}
    if lease['generation']!=x['generation']:return {'decision':'lease_generation_changed'}
    if lease['pod_uid']!=x['pod_uid'] or lease['node_uid']!=x['node_uid']:return {'decision':'stale_holder'}
    if one(db,"SELECT 1 FROM recovery_permits WHERE lease_id=? AND status='activated'",(lease['id'],)):
        return {'decision':'endpoint_already_active'}
    if one(db,"SELECT 1 FROM address_leases WHERE pod_uid=? AND id!=? AND status IN ('bound','fenced')",(lease['pod_uid'],lease['id'])):
        return {'decision':'endpoint_already_active'}
    pool=one(db,'SELECT * FROM address_pools WHERE id=?',(lease['pool_id'],))
    claim=one(db,'SELECT * FROM tenant_claims WHERE id=?',(lease['claim_id'],))
    if not pool or pool['status']!='active' or not claim or pool['tenant']!=claim['tenant'] or pool['generation']!=lease['pool_generation']:
        return {'decision':'pool_ownership_changed'}
    if op=='restore':return restore(db,x,conflict,lease)
    return replace(db,x,conflict,lease,pool,claim)


def restore(db,x,conflict,lease):
    if conflict['status']!='resolved':return {'decision':'conflict_unresolved'}
    if one(db,"SELECT 1 FROM address_conflicts WHERE ip=? AND status='isolated'",(lease['ip'],)):
        return {'decision':'conflict_unresolved'}
    if one(db,"SELECT 1 FROM orphan_addresses WHERE ip=? AND status='isolated'",(lease['ip'],)):
        return {'decision':'orphan_remains'}
    if one(db,"SELECT 1 FROM address_leases WHERE ip=? AND id!=? AND status IN ('bound','fenced')",(lease['ip'],lease['id'])):
        return {'decision':'address_reassigned'}
    db.execute("UPDATE address_leases SET status='bound' WHERE id=?",(lease['id'],))
    record(db,x,conflict,lease,'original',lease['ip'])
    return {'decision':'original_restored','ip':lease['ip']}


def replace(db,x,conflict,lease,pool,claim):
    if claim['status']!='active' or claim['pool_generation']!=pool['generation'] or claim['expires']<=x['at']:
        return {'decision':'source_unavailable'}
    try: ip=ipaddress.ip_address(x['ip'])
    except ValueError:return {'decision':'invalid_ip'}
    network=ipaddress.ip_network(pool['network'])
    if ip not in network or ip in (network.network_address,network.broadcast_address) or x['pool_generation']!=pool['generation']:
        return {'decision':'address_outside_current_pool'}
    count=one(db,"SELECT COUNT(*) n FROM address_leases WHERE claim_id=? AND status IN ('bound','fenced','isolated')",(claim['id'],))['n']
    if count>=claim['capacity']:return {'decision':'claim_full'}
    if one(db,"SELECT 1 FROM address_leases WHERE ip=? AND status IN ('bound','fenced','isolated')",(x['ip'],)):
        return {'decision':'address_occupied'}
    if one(db,"SELECT 1 FROM orphan_addresses WHERE ip=? AND status='isolated'",(x['ip'],)):
        return {'decision':'orphan_address_blocked'}
    db.execute('INSERT INTO address_leases VALUES(?,?,?,?,?,?,?,?,?)',(x['new_lease'],pool['id'],claim['id'],x['ip'],lease['pod_uid'],lease['node_uid'],pool['generation'],1,'bound'))
    record(db,x,conflict,lease,'replacement',x['ip'])
    return {'decision':'replacement_bound','ip':x['ip'],'lease':x['new_lease']}


def record(db,x,conflict,lease,mode,ip):
    db.execute('INSERT INTO recovery_permits VALUES(?,?,?,?,?,?,?,?,?,?,?)',(x['id'],conflict['id'],lease['id'],lease['generation'],lease['pod_uid'],lease['node_uid'],mode,ip,x['independent_receipt'],x.get('at'),'activated'))
