DEMO_MODULES = ('pool', 'lease', 'reclaim')
MODULES = (*DEMO_MODULES, 'fabric', 'route_update')
DEMO = [(name, f'fixtures/{name}_normal.json') for name in DEMO_MODULES] + [
    (name, f'fixtures/{name}_failure.json') for name in DEMO_MODULES
] + [('reclaim', 'fixtures/path.json')]
