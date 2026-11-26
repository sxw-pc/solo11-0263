import json
import tempfile
import unittest
from pathlib import Path
import ipleasecourt
from ipleasecourt.store import connect,act,replay,one

ROOT=Path(__file__).resolve().parents[1]

class Business(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'state.sqlite';self.db=connect(str(self.path))
    def tearDown(self):self.db.close();self.tmp.cleanup()
    def fixture(self,name):
        module=name.split('_')[0]
        return [act(self.db,module,x) for x in json.loads((ROOT/'fixtures'/f'{name}.json').read_text(encoding='utf-8'))]
    def base(self):
        for name in ('pool_normal','lease_normal','reclaim_normal'):self.fixture(name)

    def test_pool_overlap_and_live_lease_block_transfer(self):
        self.fixture('pool_normal');self.fixture('lease_normal')
        results=self.fixture('pool_failure')
        self.assertEqual(results[0]['decision'],'overlapping_pool')
        self.assertEqual(results[1]['decision'],'transfer_blocked_by_leases')

    def test_fenced_old_holder_cannot_renew_or_double_issue(self):
        self.fixture('pool_normal');self.fixture('lease_normal')
        results=self.fixture('lease_failure')
        self.assertEqual([r['decision'] for r in results],['stale_holder','address_occupied'])
        self.assertEqual(one(self.db,"SELECT status FROM address_leases WHERE id='lease-a'")['status'],'fenced')

    def test_missing_gateway_and_old_generation_block_reclaim(self):
        self.fixture('pool_normal');self.fixture('lease_normal')
        steps=json.loads((ROOT/'fixtures/reclaim_normal.json').read_text(encoding='utf-8'))
        for step in steps[:2]:act(self.db,'reclaim',step)
        self.assertEqual(act(self.db,'reclaim',dict(steps[4],event_id='early-close'))['decision'],'gateway_gap')
        self.assertEqual(act(self.db,'reclaim',{'event_id':'old-gw','op':'gateway','id':'old','case':'reclaim-a','gateway':'gateway-b','generation':1,'at':31})['decision'],'stale_gateway_receipt')
        for step in steps[2:4]:act(self.db,'reclaim',step)
        self.assertEqual(act(self.db,'reclaim',steps[4])['decision'],'address_reusable')

    def test_late_connection_reopens_closed_case_and_requires_new_drain(self):
        self.base()
        results=self.fixture('reclaim_failure')
        self.assertEqual(results[1]['decision'],'late_connection_reopened')
        self.assertEqual(results[2]['decision'],'connections_remain')
        self.assertEqual(results[-1]['decision'],'address_reusable')

    def test_orphan_after_reuse_reopens_and_isolates(self):
        self.base();self.fixture('reclaim_failure')
        outcomes=[act(self.db,'reclaim',x) for x in json.loads((ROOT/'fixtures/path.json').read_text(encoding='utf-8'))]
        self.assertEqual(outcomes[0]['decision'],'orphan_isolated')
        self.assertEqual(one(self.db,"SELECT status FROM reclaim_cases WHERE id='reclaim-a'")['status'],'reopened')
        self.assertEqual(outcomes[1]['decision'],'orphan_remains')
        self.assertEqual(act(self.db,'reclaim',{'event_id':'orphan-clear','op':'resolve_orphan','id':'orphan-a','independent_receipt':'node-and-gateway-proof'})['decision'],'orphan_cleared')
        self.assertEqual(act(self.db,'reclaim',{'event_id':'close-after-clear','op':'close','case':'reclaim-a'})['decision'],'address_reusable')

    def test_claim_expiry_only_stops_new_addresses(self):
        self.fixture('pool_normal');self.fixture('lease_normal')
        result=act(self.db,'pool',{'event_id':'expire','op':'expire_claim','id':'claim-a','at':101})
        self.assertEqual(result['decision'],'new_allocations_stopped')
        self.assertEqual(one(self.db,"SELECT status FROM address_leases WHERE id='lease-a'")['status'],'fenced')
        attempt={'event_id':'late-issue','op':'issue','id':'lease-b','pool':'pool-a','claim':'claim-a','ip':'10.4.0.3','pod_uid':'pod-b','node_uid':'node-b','pool_generation':1,'at':102}
        self.assertEqual(act(self.db,'lease',attempt)['decision'],'source_unavailable')

    def test_all_fixtures_replay_and_module_origin(self):
        self.base()
        for name in ('pool_failure','lease_failure','reclaim_failure'):self.fixture(name)
        for x in json.loads((ROOT/'fixtures/path.json').read_text(encoding='utf-8')):act(self.db,'reclaim',x)
        self.assertEqual(Path(ipleasecourt.__file__).resolve().parents[1],ROOT)
        result=replay(str(self.path));self.assertTrue(result['verified'],result)
        self.assertEqual(result['decisions'],20)

print('module_file',ipleasecourt.__file__)
