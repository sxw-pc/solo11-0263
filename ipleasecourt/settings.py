MODULES=('pool','lease','reclaim','recovery')
DEMO_MODULES=('pool','lease','reclaim')
DEMO=[(name,f'fixtures/{name}_normal.json') for name in DEMO_MODULES]+[(name,f'fixtures/{name}_failure.json') for name in DEMO_MODULES]+[('reclaim','fixtures/path.json')]
