import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from ipleasecourt.store import act, connect, replay


class WorkflowTest(unittest.TestCase):
    def test_cli_and_replay(self):
        with tempfile.TemporaryDirectory() as temp:
            database = str(Path(temp) / 'state.sqlite')
            with closing(connect(database)) as db:
                init = json.loads(Path('fixtures/pool_normal.json').read_text(encoding='utf-8'))
                for row in init:
                    act(db, 'pool', row)
                issue = json.loads(Path('fixtures/lease_normal.json').read_text(encoding='utf-8'))[0]
                self.assertEqual(act(db, 'lease', issue)['decision'], 'bound')
                second = dict(issue, event_id='lease-b', id='lease-b', ip='10.4.0.3', pod_uid='pod-b', node_uid='node-b')
                self.assertEqual(act(db, 'lease', second)['decision'], 'bound')
            command = [sys.executable, '-B', '-m', 'ipleasecourt', 'fabric', '--db', database,
                       '--fixture', 'fixtures/fabric_examples.json']
            result = json.loads(subprocess.check_output(command, encoding='utf-8'))[0]['results']
            self.assertEqual([row['decision'] for row in result], ["fabric_link_active","fabric_link_active","fabric_link_active","fabric_routed","fabric_delivered","fabric_link_down","fabric_route_broken","fabric_routed"])
            self.assertEqual(result[3]['links'], ['nx','xd'])
            self.assertEqual(result[-1]['links'], ['nd'])
            self.assertEqual(result, json.loads(subprocess.check_output(command, encoding='utf-8'))[0]['results'])
            self.assertTrue(replay(database)['verified'])
