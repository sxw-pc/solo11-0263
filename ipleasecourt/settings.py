MODULES=('pool','lease','reclaim')
DEMO=[(name,f'fixtures/{name}_normal.json') for name in MODULES]+[(name,f'fixtures/{name}_failure.json') for name in MODULES]+[('reclaim','fixtures/path.json')]
