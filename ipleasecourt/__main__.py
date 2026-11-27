import argparse
import json
from pathlib import Path
from .settings import MODULES, DEMO
from .store import act, connect, replay, many

OP_SUBSYSTEM = {
    'publish': 'pool', 'claim': 'pool', 'expire_claim': 'pool', 'transfer': 'pool',
    'issue': 'lease', 'renew': 'lease', 'fence': 'lease', 'conflict': 'lease',
    'link': 'fabric', 'down': 'fabric', 'route': 'fabric', 'send': 'fabric',
    'prepare': 'route_update', 'install': 'route_update', 'witness': 'route_update',
    'pause': 'route_update', 'inject': 'route_update', 'forward': 'route_update',
    'drain': 'route_update', 'finish': 'route_update', 'inflight': 'route_update',
}


def main():
    p = argparse.ArgumentParser()
    p.add_argument('command', choices=[*MODULES, 'demo', 'replay', 'report'])
    p.add_argument('--db', default='state.sqlite')
    p.add_argument('--fixture')
    args = p.parse_args()
    if args.command == 'replay':
        result = replay(args.db)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if not result['verified']:
            raise SystemExit(2)
        return
    db = connect(args.db)
    try:
        if args.command == 'report':
            result = many(db, 'SELECT * FROM events ORDER BY seq')
        else:
            files = DEMO if args.command == 'demo' else [(args.command, args.fixture)]
            if any(not file for _, file in files):
                p.error('--fixture required')
            result = []
            for subsystem, file in files:
                steps = json.loads(Path(file).read_text(encoding='utf-8'))
                outcomes = []
                for step in steps:
                    # Cross-subsystem fixtures (e.g. route cutover) dispatch
                    # each row by operation; single-subsystem fixtures still
                    # resolve to their owning module exactly as before.
                    target = OP_SUBSYSTEM.get(step['op'], subsystem)
                    outcomes.append(act(db, target, step))
                result.append({'fixture': file, 'results': outcomes})
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        db.close()


if __name__ == '__main__':
    main()
