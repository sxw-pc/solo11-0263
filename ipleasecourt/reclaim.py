from .store import one,many

SCHEMA='''
CREATE TABLE IF NOT EXISTS reclaim_cases(id TEXT PRIMARY KEY,lease_id TEXT,lease_generation INTEGER,expected_gateways INTEGER,status TEXT);
CREATE TABLE IF NOT EXISTS gateway_receipts(id TEXT PRIMARY KEY,case_id TEXT,gateway TEXT,lease_generation INTEGER,at INTEGER,status TEXT);
CREATE TABLE IF NOT EXISTS connection_evidence(id TEXT PRIMARY KEY,case_id TEXT,observed INTEGER,state TEXT);
CREATE TABLE IF NOT EXISTS orphan_addresses(id TEXT PRIMARY KEY,ip TEXT,source TEXT,status TEXT);
'''


def handle(db,x):
    op=x['op']
    if op=='open':
        lease=one(db,'SELECT * FROM address_leases WHERE id=?',(x['lease'],))
        fence=one(db,"SELECT * FROM holder_fences WHERE lease_id=? AND generation=? AND status='active'",(x['lease'],x['generation']))
        if not lease or not fence or lease['status']!='fenced' or x['gateways']<1:return {'decision':'unfenced_holder'}
        db.execute('INSERT INTO reclaim_cases VALUES(?,?,?,?,?)',(x['id'],x['lease'],x['generation'],x['gateways'],'waiting'))
        return {'decision':'reclaim_open'}
    if op=='gateway':
        case=one(db,'SELECT * FROM reclaim_cases WHERE id=?',(x['case'],))
        if not case or case['status']!='waiting' or case['lease_generation']!=x['generation']:
            return {'decision':'stale_gateway_receipt'}
        if one(db,'SELECT 1 FROM gateway_receipts WHERE case_id=? AND gateway=?',(x['case'],x['gateway'])):
            return {'decision':'gateway_already_counted'}
        db.execute('INSERT INTO gateway_receipts VALUES(?,?,?,?,?,?)',(x['id'],x['case'],x['gateway'],x['generation'],x['at'],'withdrawn'))
        return {'decision':'route_withdrawn'}
    if op=='connection':
        case=one(db,'SELECT * FROM reclaim_cases WHERE id=?',(x['case'],))
        if not case:return {'decision':'unknown_case'}
        db.execute('INSERT INTO connection_evidence VALUES(?,?,?,?)',(x['id'],x['case'],x['at'],x['state']))
        if x['state']=='active' and case['status']=='complete':
            db.execute("UPDATE reclaim_cases SET status='reopened' WHERE id=?",(x['case'],))
            db.execute("UPDATE address_leases SET status='isolated' WHERE id=?",(case['lease_id'],))
            return {'decision':'late_connection_reopened'}
        return {'decision':'connection_recorded'}
    if op=='close':
        case=one(db,'SELECT * FROM reclaim_cases WHERE id=?',(x['case'],))
        if not case or case['status'] not in ('waiting','reopened'):return {'decision':'case_unavailable'}
        lease=one(db,'SELECT * FROM address_leases WHERE id=?',(case['lease_id'],))
        if not lease or lease['generation']!=case['lease_generation']:
            return {'decision':'lease_generation_changed'}
        count=one(db,"SELECT COUNT(DISTINCT gateway) n FROM gateway_receipts WHERE case_id=? AND lease_generation=? AND status='withdrawn'",(x['case'],case['lease_generation']))['n']
        if count<case['expected_gateways']:return {'decision':'gateway_gap','missing':case['expected_gateways']-count}
        latest=one(db,'SELECT * FROM connection_evidence WHERE case_id=? ORDER BY observed DESC,id DESC LIMIT 1',(x['case'],))
        if not latest or latest['state']!='drained':return {'decision':'connections_remain'}
        if one(db,"SELECT 1 FROM address_conflicts WHERE ip=? AND status='isolated'",(lease['ip'],)):
            return {'decision':'holder_conflict_remains'}
        if one(db,"SELECT 1 FROM orphan_addresses WHERE ip=? AND status='isolated'",(lease['ip'],)):
            return {'decision':'orphan_remains'}
        db.execute("UPDATE reclaim_cases SET status='complete' WHERE id=?",(x['case'],))
        db.execute("UPDATE address_leases SET status='released' WHERE id=?",(lease['id'],))
        return {'decision':'address_reusable','ip':lease['ip']}
    if op=='orphan':
        db.execute('INSERT INTO orphan_addresses VALUES(?,?,?,?)',(x['id'],x['ip'],x['source'],'isolated'))
        lease=one(db,"SELECT * FROM address_leases WHERE ip=? AND status='released' ORDER BY generation DESC LIMIT 1",(x['ip'],))
        if lease:
            db.execute("UPDATE address_leases SET status='isolated' WHERE id=?",(lease['id'],))
            db.execute("UPDATE reclaim_cases SET status='reopened' WHERE lease_id=? AND status='complete'",(lease['id'],))
        return {'decision':'orphan_isolated'}
    if op=='resolve_conflict':
        conflict=one(db,'SELECT * FROM address_conflicts WHERE id=?',(x['id'],))
        if not conflict or not x.get('independent_receipt'):return {'decision':'resolution_denied'}
        db.execute("UPDATE address_conflicts SET status='resolved' WHERE id=?",(x['id'],))
        return {'decision':'conflict_resolved'}
    if op=='resolve_orphan':
        orphan=one(db,'SELECT * FROM orphan_addresses WHERE id=?',(x['id'],))
        if not orphan or orphan['status']!='isolated' or not x.get('independent_receipt'):
            return {'decision':'orphan_resolution_denied'}
        db.execute("UPDATE orphan_addresses SET status='cleared' WHERE id=?",(x['id'],))
        return {'decision':'orphan_cleared'}
    return {'decision':'unknown_operation'}
