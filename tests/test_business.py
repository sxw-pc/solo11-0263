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

    def test_orphan_without_registered_lease_blocks_new_allocation(self):
        self.fixture('pool_normal')
        self.assertEqual(act(self.db,'reclaim',{'event_id':'unregistered','op':'orphan','id':'orphan-x','ip':'10.4.0.3','source':'node-report'})['decision'],'orphan_isolated')
        attempt={'event_id':'blocked-by-orphan','op':'issue','id':'lease-x','pool':'pool-a','claim':'claim-a','ip':'10.4.0.3','pod_uid':'pod-x','node_uid':'node-x','pool_generation':1,'at':20}
        self.assertEqual(act(self.db,'lease',attempt)['decision'],'orphan_address_blocked')
        self.assertEqual(act(self.db,'reclaim',{'event_id':'clear-unregistered','op':'resolve_orphan','id':'orphan-x','independent_receipt':'network-clear'})['decision'],'orphan_cleared')
        self.assertEqual(act(self.db,'lease',dict(attempt,event_id='issued-after-clear'))['decision'],'bound')

    def test_conflicting_holder_evidence_isolated_before_reclaim(self):
        self.fixture('pool_normal');self.fixture('lease_normal')
        self.assertEqual(act(self.db,'lease',{'event_id':'conflict','op':'conflict','id':'conflict-a','ip':'10.4.0.2','other_pod':'pod-z'})['decision'],'address_isolated')
        self.assertEqual(one(self.db,"SELECT status FROM address_leases WHERE id='lease-a'")['status'],'isolated')
        self.assertEqual(act(self.db,'reclaim',{'event_id':'resolve-conflict','op':'resolve_conflict','id':'conflict-a','independent_receipt':'two-node-proof'})['decision'],'conflict_resolved')

    def test_claim_expiry_only_stops_new_addresses(self):
        self.fixture('pool_normal');self.fixture('lease_normal')
        result=act(self.db,'pool',{'event_id':'expire','op':'expire_claim','id':'claim-a','at':101})
        self.assertEqual(result['decision'],'new_allocations_stopped')
        self.assertEqual(one(self.db,"SELECT status FROM address_leases WHERE id='lease-a'")['status'],'fenced')
        attempt={'event_id':'late-issue','op':'issue','id':'lease-b','pool':'pool-a','claim':'claim-a','ip':'10.4.0.3','pod_uid':'pod-b','node_uid':'node-b','pool_generation':1,'at':102}
        self.assertEqual(act(self.db,'lease',attempt)['decision'],'source_unavailable')

    def test_pool_transfer_invalidates_old_tenant_claim_even_before_expiry(self):
        self.base()
        self.assertEqual(act(self.db,'pool',{'event_id':'transfer-b','op':'transfer','id':'transfer-b','pool':'pool-a','tenant':'team-b','generation':2})['decision'],'transferred')
        old={'event_id':'old-team-issue','op':'issue','id':'old-team-ip','pool':'pool-a','claim':'claim-a','ip':'10.4.0.3','pod_uid':'pod-old','node_uid':'node-old','pool_generation':2,'at':40}
        self.assertEqual(act(self.db,'lease',old)['decision'],'source_unavailable')
        self.assertEqual(act(self.db,'pool',{'event_id':'new-team-claim','op':'claim','id':'claim-b','pool':'pool-a','tenant':'team-b','capacity':2,'expires':100,'at':40})['decision'],'claim_active')
        new=dict(old,event_id='new-team-issue',id='new-team-ip',claim='claim-b',pod_uid='pod-new')
        self.assertEqual(act(self.db,'lease',new)['decision'],'bound')

    def test_all_fixtures_replay_and_module_origin(self):
        self.base()
        for name in ('pool_failure','lease_failure','reclaim_failure'):self.fixture(name)
        for x in json.loads((ROOT/'fixtures/path.json').read_text(encoding='utf-8')):act(self.db,'reclaim',x)
        self.assertEqual(Path(ipleasecourt.__file__).resolve().parents[1],ROOT)
        result=replay(str(self.path));self.assertTrue(result['verified'],result)
        self.assertEqual(result['decisions'],20)

print('module_file',ipleasecourt.__file__)
