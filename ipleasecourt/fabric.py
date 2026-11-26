"""Offline directional link inventory and forwarding for bound endpoints."""
import heapq
import json

from .store import many, one

SCHEMA = '''
CREATE TABLE IF NOT EXISTS links(
    id TEXT PRIMARY KEY, origin TEXT NOT NULL, destination TEXT NOT NULL,
    cost INTEGER NOT NULL CHECK(cost>0), active INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS flow_routes(
    id TEXT PRIMARY KEY, source TEXT NOT NULL, source_generation INTEGER NOT NULL,
    destination TEXT NOT NULL, destination_generation INTEGER NOT NULL, route TEXT NOT NULL
);
'''


def endpoint(db, lease_id):
    return one(db, "SELECT * FROM address_leases WHERE id=? AND status='bound'", (lease_id,))


def plan(db, origin, destination):
    links = many(db, 'SELECT * FROM links WHERE active=1 ORDER BY id')
    outgoing = {}
    for link in links:
        outgoing.setdefault(link['origin'], []).append(link)
    queue = [(0, (), origin)]
    visited = set()
    while queue:
        cost, route, node = heapq.heappop(queue)
        if node in visited:
            continue
        visited.add(node)
        if node == destination:
            return list(route)
        for link in outgoing.get(node, []):
            heapq.heappush(queue, (cost + link['cost'], route + (link['id'],), link['destination']))
    return None


def handle(db, command):
    operation = command['op']
    if operation == 'link':
        cost = command['cost']
        if type(cost) is not int or cost < 1 or command['from'] == command['to']:
            return {'decision': 'invalid_fabric_link'}
        if one(db, 'SELECT id FROM links WHERE id=?', (command['id'],)):
            return {'decision': 'fabric_link_exists'}
        db.execute('INSERT INTO links VALUES(?,?,?,?,1)', (command['id'], command['from'], command['to'], cost))
        return {'decision': 'fabric_link_active'}
    if operation == 'down':
        result = db.execute('UPDATE links SET active=0 WHERE id=? AND active=1', (command['id'],))
        return {'decision': 'fabric_link_down' if result.rowcount else 'fabric_link_unavailable'}
    if operation == 'route':
        if one(db, 'SELECT id FROM flow_routes WHERE id=?', (command['id'],)):
            return {'decision': 'fabric_flow_exists'}
        src, dst = (endpoint(db, command[key]) for key in ('source', 'destination'))
        if not src or not dst:
            return {'decision': 'fabric_endpoint_unavailable'}
        route = plan(db, src['node_uid'], dst['node_uid'])
        if route is None:
            return {'decision': 'fabric_unreachable'}
        db.execute('INSERT INTO flow_routes VALUES(?,?,?,?,?,?)', (command['id'], src['id'], src['generation'], dst['id'], dst['generation'], json.dumps(route)))
        return {'decision': 'fabric_routed', 'links': route}
    if operation == 'send':
        flow = one(db, 'SELECT * FROM flow_routes WHERE id=?', (command['id'],))
        if not flow:
            return {'decision': 'fabric_flow_unavailable'}
        for key in ('source', 'destination'):
            lease = endpoint(db, flow[key])
            if not lease or lease['generation'] != flow[key + '_generation']:
                return {'decision': 'fabric_endpoint_unavailable'}
        route = json.loads(flow['route'])
        for link_id in route:
            if not one(db, 'SELECT id FROM links WHERE id=? AND active=1', (link_id,)):
                return {'decision': 'fabric_route_broken'}
        return {'decision': 'fabric_delivered', 'links': route}
    return {'decision': 'unknown_operation'}
