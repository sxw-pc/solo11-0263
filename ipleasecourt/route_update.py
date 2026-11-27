"""Per-node forwarding-table cutover with hop-by-hop packet verification.

The fixed offline route in ``fabric.py`` only reports ``fabric_route_broken``
after a link is lost.  This module performs the actual forwarding-table
handover, node by node, and proves with simulated packets that in-flight
traffic keeps arriving while the tables are mixed.

Two cutover modes are supported, and they must never be conflated:

* ``dual``  - maintenance authorization allows old and new tables to be
              effective at the same time.  Packets carry a tag and every
              node keeps both entries until the cutover completes, so an
              old-tag packet always follows its original route and a
              new-tag packet always follows the new route.
* ``single``- only one table slot exists.  The source must authorize a
              short pause; genuinely undelivered packets are drained while
              the old table is still effective, and only then is the new
              table made effective.  A late *install receipt* never proves
              the old table was cleared: a separate *effective witness* is
              required per node.

Writing a table (an install receipt) and the table becoming effective for
data traffic (an effective witness) are recorded as distinct facts.  A
cutover cannot be declared complete without an effective witness from
every forwarding node.
"""
import json

from .store import many, one

SCHEMA = '''
CREATE TABLE IF NOT EXISTS route_updates(
    id TEXT PRIMARY KEY, flow_id TEXT NOT NULL, mode TEXT NOT NULL,
    source_node TEXT NOT NULL, dest_node TEXT NOT NULL,
    dest_lease TEXT NOT NULL, dest_generation INTEGER NOT NULL,
    old_route TEXT NOT NULL, new_route TEXT NOT NULL,
    forwarding_nodes TEXT NOT NULL, status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS forwarding_installs(
    update_id TEXT NOT NULL, node TEXT NOT NULL, tag TEXT NOT NULL,
    installed INTEGER NOT NULL, effective INTEGER NOT NULL,
    PRIMARY KEY(update_id, node, tag)
);
CREATE TABLE IF NOT EXISTS route_packets(
    update_id TEXT NOT NULL, packet_id TEXT NOT NULL, tag TEXT NOT NULL,
    dest_lease TEXT NOT NULL, dest_generation INTEGER NOT NULL,
    at_node TEXT, delivered INTEGER NOT NULL, held INTEGER NOT NULL,
    trace TEXT NOT NULL, PRIMARY KEY(update_id, packet_id)
);
'''

DUAL = 'dual'
SINGLE = 'single'
TAGS = ('old', 'new')


def _load_links(db, link_ids):
    """Resolve active links for a link-id route, preserving order."""
    by_id = {link['id']: link for link in many(db, 'SELECT * FROM links WHERE active=1')}
    route = []
    for link_id in link_ids:
        link = by_id.get(link_id)
        if link is None:
            return None
        route.append(link)
    return route


def _route_nodes(route):
    """Flatten a link route into the ordered nodes it traverses."""
    nodes = [route[0]['origin']]
    for link in route:
        if link['origin'] != nodes[-1]:
            return None
        nodes.append(link['destination'])
    return nodes


def _load_path(db, link_ids):
    route = _load_links(db, link_ids)
    if route is None:
        return None
    return _route_nodes(route)


def _next_hop(nodes, node):
    index = nodes.index(node)
    return None if index == len(nodes) - 1 else nodes[index + 1]


def _bound_lease(db, lease_id):
    return one(db, "SELECT * FROM address_leases WHERE id=? AND status='bound'", (lease_id,))


def _update(db, update_id):
    return one(db, 'SELECT * FROM route_updates WHERE id=?', (update_id,))


def _packet(db, update_id, packet_id):
    return one(db, 'SELECT * FROM route_packets WHERE update_id=? AND packet_id=?',
               (update_id, packet_id))


def _effective(db, update_id, node, tag):
    row = one(db, 'SELECT effective FROM forwarding_installs '
                  'WHERE update_id=? AND node=? AND tag=?', (update_id, node, tag))
    return bool(row and row['effective'])


def _save_packet(db, update, packet_id, tag, at_node, delivered, held, trace):
    db.execute(
        'INSERT INTO route_packets VALUES(?,?,?,?,?,?,?,?,?) '
        'ON CONFLICT(update_id,packet_id) DO UPDATE SET at_node=excluded.at_node,'
        'delivered=excluded.delivered,held=excluded.held,trace=excluded.trace',
        (update['id'], packet_id, tag, update['dest_lease'], update['dest_generation'],
         at_node, int(delivered), int(held), json.dumps(trace)))


def _lookup_next_hop(db, update, node, tag):
    """Decide where a tagged packet at ``node`` must be forwarded.

    Returns ``(next_node, reason)`` with reason in
    ``forward | delivered | no_entry``.  The packet never falls back to
    the other tag's table: falling back on a mixed old/new network is how
    a packet gets bounced back through a node it already visited.
    """
    old_nodes = _route_nodes(_load_links(db, json.loads(update['old_route'])))
    new_nodes = _route_nodes(_load_links(db, json.loads(update['new_route'])))
    forwarding_nodes = json.loads(update['forwarding_nodes'])

    if update['mode'] == DUAL:
        if node in forwarding_nodes and not _effective(db, update['id'], node, tag):
            return None, 'no_entry'
        path = old_nodes if tag == 'old' else new_nodes
    else:
        if update['status'] == 'complete':
            path = new_nodes
        else:
            # Single slot during the handover: the old table stays
            # effective everywhere.  New-tag traffic is stopped at ingress
            # and must not be forwarded on a half-switched node.
            if tag == 'new':
                return None, 'no_entry'
            path = old_nodes

    if node == path[-1]:
        return None, 'delivered'
    return _next_hop(path, node), 'forward'


def _advance(db, update, packet_id):
    """Forward one hop; hold when the node's tagged table is not ready."""
    packet = _packet(db, update['id'], packet_id)
    tag = packet['tag']
    trace = json.loads(packet['trace'])
    node = packet['at_node']

    # The generation guard applies at every step: if the endpoint was
    # fenced or moved to another lease generation, an in-flight old label
    # must be stopped before it can enter that endpoint.
    dst = _bound_lease(db, update['dest_lease'])
    endpoint_live = bool(dst) and dst['generation'] == update['dest_generation']
    if not endpoint_live:
        _save_packet(db, update, packet_id, tag, node, False, True, trace)
        return {'decision': 'endpoint_generation_fenced', 'packet_id': packet_id,
                'at_node': node, 'trace': trace}

    next_node, reason = _lookup_next_hop(db, update, node, tag)

    if reason == 'delivered':
        trace.append(node + ':delivered')
        _save_packet(db, update, packet_id, tag, node, True, False, trace)
        return {'decision': 'packet_delivered', 'packet_id': packet_id,
                'at_node': node, 'trace': trace}

    if reason == 'no_entry':
        _save_packet(db, update, packet_id, tag, node, False, True, trace)
        return {'decision': 'packet_held_awaiting_table', 'packet_id': packet_id,
                'at_node': node, 'tag': tag, 'trace': trace}

    if next_node in {step.replace(':delivered', '') for step in trace}:
        # Defensive: consistent tag selection should make this unreachable.
        _save_packet(db, update, packet_id, tag, node, False, True, trace)
        return {'decision': 'forwarding_loop_blocked', 'packet_id': packet_id,
                'at_node': node, 'next_node': next_node, 'trace': trace}

    trace.append(next_node)
    _save_packet(db, update, packet_id, tag, next_node, False, False, trace)
    return {'decision': 'packet_forwarded', 'packet_id': packet_id,
            'at_node': next_node, 'tag': tag, 'trace': trace}


def handle(db, command):
    op = command['op']

    if op == 'prepare':
        if _update(db, command['id']):
            return {'decision': 'route_update_exists'}
        mode = command.get('mode', DUAL)
        if mode not in (DUAL, SINGLE):
            return {'decision': 'invalid_cutover_mode'}
        flow = one(db, 'SELECT * FROM flow_routes WHERE id=?', (command['flow'],))
        if not flow:
            return {'decision': 'fabric_flow_unavailable'}
        src = _bound_lease(db, flow['source'])
        dst = _bound_lease(db, flow['destination'])
        if not src or not dst:
            return {'decision': 'fabric_endpoint_unavailable'}
        old_link_ids = json.loads(flow['route'])
        old_nodes = _load_path(db, old_link_ids)
        new_nodes = _load_path(db, command['new_route'])
        if old_nodes is None or new_nodes is None:
            return {'decision': 'fabric_route_broken'}
        if old_nodes[0] != new_nodes[0] or old_nodes[-1] != new_nodes[-1]:
            return {'decision': 'route_endpoints_mismatch'}
        if old_nodes == new_nodes:
            return {'decision': 'route_unchanged'}
        if dst['generation'] != flow['destination_generation']:
            # Pin the destination lease generation so an old label can
            # never enter a different lease that later takes the endpoint.
            return {'decision': 'fabric_endpoint_unavailable'}
        forwarding_nodes = old_nodes[1:-1]
        if not forwarding_nodes:
            return {'decision': 'route_without_forwarding_nodes'}
        db.execute(
            'INSERT INTO route_updates VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (command['id'], flow['id'], mode, old_nodes[0], old_nodes[-1],
             dst['id'], dst['generation'], json.dumps(old_link_ids),
             json.dumps(command['new_route']), json.dumps(forwarding_nodes),
             'prepared'))
        for fwd_node in forwarding_nodes:
            # Both slots exist from the start.  Old tables are already
            # installed and effective (that is the live baseline); the new
            # slot waits for explicit receipts and witnesses.
            db.execute('INSERT INTO forwarding_installs VALUES(?,?,?,?,?)',
                       (command['id'], fwd_node, 'old', 1, 1))
            db.execute('INSERT INTO forwarding_installs VALUES(?,?,?,?,?)',
                       (command['id'], fwd_node, 'new', 0, 0))
        return {'decision': 'cutover_prepared',
                'old_nodes': old_nodes, 'new_nodes': new_nodes,
                'forwarding_nodes': forwarding_nodes}

    update = _update(db, command.get('update_id', command.get('id', '')))
    if not update:
        return {'decision': 'unknown_route_update'}

    if op == 'install':
        # A table-write receipt.  It never implies effectiveness.
        tag = command['tag']
        if tag not in TAGS:
            return {'decision': 'invalid_packet_tag'}
        if update['mode'] == SINGLE and tag == 'new' and update['status'] == 'prepared':
            # The single-slot new table may only be written after the
            # authorized ingress pause.  Once paused, retried receipts are
            # accepted so an interrupted install can resume.
            return {'decision': 'pause_required_before_install'}
        row = one(db, 'SELECT * FROM forwarding_installs WHERE update_id=? AND node=? AND tag=?',
                  (update['id'], command['node'], tag))
        if not row:
            return {'decision': 'node_not_in_cutover'}
        if row['effective']:
            return {'decision': 'table_already_effective'}
        if row['installed']:
            return {'decision': 'install_receipt_already_recorded'}
        db.execute('UPDATE forwarding_installs SET installed=1 '
                   'WHERE update_id=? AND node=? AND tag=?',
                   (update['id'], command['node'], tag))
        return {'decision': 'table_install_receipt_recorded',
                'node': command['node'], 'tag': tag, 'effective': False}

    if op == 'witness':
        # The separate fact that the table actually forwards data traffic.
        tag = command['tag']
        if tag not in TAGS:
            return {'decision': 'invalid_packet_tag'}
        row = one(db, 'SELECT * FROM forwarding_installs WHERE update_id=? AND node=? AND tag=?',
                  (update['id'], command['node'], tag))
        if not row:
            return {'decision': 'node_not_in_cutover'}
        if not row['installed']:
            # An effectiveness claim without a write receipt (e.g. a late
            # or spoofed reply) cannot be accepted.
            return {'decision': 'witness_without_install'}
        if not row['effective']:
            db.execute('UPDATE forwarding_installs SET effective=1 '
                       'WHERE update_id=? AND node=? AND tag=?',
                       (update['id'], command['node'], tag))
        return {'decision': 'table_effective_witness_recorded',
                'node': command['node'], 'tag': tag, 'effective': True}

    if op == 'pause':
        if update['mode'] != SINGLE:
            return {'decision': 'pause_not_permitted'}
        if update['status'] not in ('prepared', 'paused'):
            return {'decision': 'cutover_state_conflict'}
        db.execute("UPDATE route_updates SET status='paused' WHERE id=?", (update['id'],))
        return {'decision': 'ingress_paused'}

    if op == 'inject':
        tag = command['tag']
        if tag not in TAGS:
            return {'decision': 'invalid_packet_tag'}
        if update['status'] not in ('prepared', 'paused', 'drained', 'complete'):
            return {'decision': 'cutover_state_conflict'}
        if (update['mode'] == SINGLE and tag == 'new'
                and update['status'] != 'complete'):
            # New traffic is refused at the paused ingress until cutover.
            return {'decision': 'ingress_paused_hold'}
        dst = _bound_lease(db, update['dest_lease'])
        if not dst:
            return {'decision': 'fabric_endpoint_unavailable'}
        if dst['generation'] != update['dest_generation']:
            # Fenced/replaced endpoint: the old label must never enter the
            # endpoint now owned by another lease generation.
            return {'decision': 'endpoint_generation_fenced'}
        if _packet(db, update['id'], command['packet_id']):
            return {'decision': 'packet_exists'}
        trace = [update['source_node']]
        _save_packet(db, update, command['packet_id'], tag,
                     update['source_node'], False, False, trace)
        return _advance(db, update, command['packet_id'])

    if op == 'forward':
        packet = _packet(db, update['id'], command['packet_id'])
        if not packet:
            return {'decision': 'unknown_packet'}
        if packet['delivered']:
            return {'decision': 'packet_already_delivered'}
        return _advance(db, update, command['packet_id'])

    if op == 'drain':
        if update['mode'] != SINGLE:
            return {'decision': 'drain_not_permitted'}
        if update['status'] != 'paused':
            return {'decision': 'pause_required_before_drain'}
        active = many(db, "SELECT packet_id FROM route_packets "
                          "WHERE update_id=? AND delivered=0 AND held=0", (update['id'],))
        held = many(db, "SELECT packet_id FROM route_packets "
                        "WHERE update_id=? AND delivered=0 AND held=1", (update['id'],))
        if active or held:
            return {'decision': 'packets_remain',
                    'in_flight': [row['packet_id'] for row in active],
                    'held': [row['packet_id'] for row in held]}
        db.execute("UPDATE route_updates SET status='drained' WHERE id=?", (update['id'],))
        return {'decision': 'ingress_drained'}

    if op == 'finish':
        if update['status'] == 'complete':
            return {'decision': 'cutover_already_complete'}
        if update['mode'] == SINGLE and update['status'] != 'drained':
            # No shortcut: the single-table cutover needs a real drain.
            return {'decision': 'drain_required_before_cutover'}
        required_tags = TAGS if update['mode'] == DUAL else ('new',)
        missing = []
        for fwd_node in json.loads(update['forwarding_nodes']):
            for tag in required_tags:
                if not _effective(db, update['id'], fwd_node, tag):
                    missing.append({'node': fwd_node, 'tag': tag})
        if missing:
            # Install receipts alone can never turn this into success.
            return {'decision': 'effective_witness_gap', 'missing': missing}
        db.execute("UPDATE route_updates SET status='complete' WHERE id=?", (update['id'],))
        return {'decision': 'cutover_complete'}

    if op == 'inflight':
        rows = many(db, "SELECT packet_id,tag,at_node,held,delivered,trace FROM route_packets "
                        "WHERE update_id=? ORDER BY packet_id", (update['id'],))
        return {'decision': 'inflight_report', 'packets': [
            {'packet_id': row['packet_id'], 'tag': row['tag'], 'at_node': row['at_node'],
             'held': bool(row['held']), 'delivered': bool(row['delivered']),
             'trace': json.loads(row['trace'])} for row in rows]}

    return {'decision': 'unknown_operation'}
