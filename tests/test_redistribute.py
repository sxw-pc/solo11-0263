import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from ipleasecourt.store import act, connect, replay


class RedistributeTest(unittest.TestCase):
    def test_capacity_moves_without_displacing_an_existing_lease(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / 'redistribute.sqlite')
            command = [sys.executable, '-B', '-m', 'ipleasecourt', 'redistribute', '--db', db_path,
                       '--fixture', 'fixtures/redistribute_examples.json']
            with closing(connect(db_path)) as db:
                for module in ('pool', 'lease'):
                    for row in json.loads(Path(f'fixtures/{module}_normal.json').read_text(encoding='utf-8')):
                        act(db, module, row)
                result = act(db, 'pool', dict(event_id='claim-b', op='claim', id='claim-b', pool='pool-a',
                                            tenant='team-a', capacity=1, expires=100, at=21))
                self.assertEqual(result['decision'], 'claim_active')
            results = json.loads(subprocess.check_output(command, encoding='utf-8'))[0]['results']
            self.assertEqual([r['decision'] for r in results], ['capacity_moved', 'capacity_needed_by_donor'])
            self.assertEqual(results, json.loads(subprocess.check_output(command, encoding='utf-8'))[0]['results'])
            with closing(connect(db_path)) as db:
                def issue(lease_id, claim, ip):
                    return act(db, 'lease', dict(event_id=lease_id, op='issue', id=lease_id, pool='pool-a',
                                                claim=claim, ip=ip, pod_uid=lease_id, node_uid='node-a', pool_generation=1, at=26))

                self.assertEqual(issue('extra-a', 'claim-a', '10.4.0.3')['decision'], 'claim_full')
                self.assertEqual(issue('first-b', 'claim-b', '10.4.0.3')['decision'], 'bound')
                self.assertEqual(issue('second-b', 'claim-b', '10.4.0.4')['decision'], 'bound')
            self.assertTrue(replay(db_path)['verified'])
