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
                pool = Path('fixtures/pool_normal.json').read_text(encoding='utf-8')
                for row in json.loads(pool):
                    act(db, 'pool', row)
                lease = Path('fixtures/lease_normal.json').read_text(encoding='utf-8')
                act(db, 'lease', json.loads(lease)[0])
            command = [sys.executable, '-B', '-m', 'ipleasecourt', 'names', '--db', database,
                       '--fixture', 'fixtures/names_examples.json']
            result = json.loads(subprocess.check_output(command, encoding='utf-8'))[0]['results']
            self.assertEqual([row['decision'] for row in result], ["name_published","name_resolved","name_unavailable"])
            self.assertEqual(result[1]['address'], '10.4.0.2')
            self.assertEqual(result, json.loads(subprocess.check_output(command, encoding='utf-8'))[0]['results'])
            self.assertTrue(replay(database)['verified'])
