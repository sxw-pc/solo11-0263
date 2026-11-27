import json
import tempfile
import unittest
from pathlib import Path
from ipleasecourt.store import connect,act,replay,one,many

ROOT=Path(__file__).resolve().parents[1]

class Recovery(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'state.sqlite';self.db=connect(str(self.path))
    def tearDown(self):self.db.close();self.tmp.cleanup()
    def run_fixture(self,name,module=None):
        steps=json.loads((ROOT/'fixtures'/f'{name}.json').read_text(encoding='utf-8'))
        return [act(self.db,module or name.split('_')[0],x) for x in steps]
    def isolate(self):
        self.run_fixture('pool_normal');self.run_fixture('lease_normal')
        result=act(self.db,'lease',{'event_id':'conflict-report','op':'conflict','id':'conflict-a','ip':'10.4.0.2','other_pod':'pod-z'})
        self.assertEqual(result['decision'],'address_isolated')
    def resolve(self,**over):
        x={'event_id':'clarify','op':'resolve_conflict','id':'conflict-a','independent_receipt':'two-node-proof'};x.update(over)
        return act(self.db,'reclaim',x)
    def restore(self,event_id,**over):
        x={'event_id':event_id,'op':'restore','id':'permit-r','conflict':'conflict-a','lease':'lease-a','generation':2,
           'pod_uid':'pod-a','node_uid':'node-a','independent_receipt':'review-board-11','at':50};x.update(over)
        return act(self.db,'recovery',x)
    def replace(self,event_id,**over):
        x={'event_id':event_id,'op':'replace','id':'permit-x','conflict':'conflict-a','lease':'lease-a','generation':2,
           'pod_uid':'pod-a','node_uid':'node-a','new_lease':'lease-b','ip':'10.4.0.3','pool_generation':1,
           'independent_receipt':'maint-ticket-7','at':50};x.update(over)
        return act(self.db,'recovery',x)
    def renew(self,event_id,lease,generation):
        return act(self.db,'lease',{'event_id':event_id,'op':'renew','id':lease,'pod_uid':'pod-a','node_uid':'node-a','generation':generation})
    def active(self):
        return many(self.db,"SELECT * FROM address_leases WHERE pod_uid='pod-a' AND status IN ('bound','fenced')")

    def test_restore_original_binding_from_fixture(self):
        self.isolate()
        self.assertEqual(self.resolve()['decision'],'conflict_resolved')
        results=self.run_fixture('restore_holder','recovery')
        self.assertEqual([r['decision'] for r in results],['lease_generation_changed','original_restored'])
        lease=one(self.db,"SELECT * FROM address_leases WHERE id='lease-a'")
        self.assertEqual((lease['status'],lease['pod_uid'],lease['node_uid'],lease['generation']),('bound','pod-a','node-a',2))
        permit=one(self.db,"SELECT * FROM recovery_permits WHERE id='permit-a'")
        self.assertEqual((permit['conflict_id'],permit['lease_id'],permit['lease_generation'],permit['holder_pod'],permit['holder_node'],permit['mode'],permit['ip'],permit['receipt']),
                         ('conflict-a','lease-a',2,'pod-a','node-a','original','10.4.0.2','review-board-11'))
        self.assertIsNone(one(self.db,"SELECT 1 FROM recovery_permits WHERE id='permit-early'"))
        self.assertEqual(one(self.db,"SELECT status FROM address_conflicts WHERE id='conflict-a'")['status'],'resolved')
        self.assertEqual(self.renew('renew-restored','lease-a',2)['decision'],'renewed')
        self.assertEqual(self.renew('renew-stale-generation','lease-a',1)['decision'],'stale_holder')
        self.assertEqual(len(self.active()),1)

    def test_restore_waits_for_case_clarification_and_holder_identity(self):
        self.isolate()
        self.assertEqual(self.restore('restore-early')['decision'],'conflict_unresolved')
        self.resolve()
        self.assertEqual(self.restore('restore-anonymous',independent_receipt=None)['decision'],'recovery_denied')
        self.assertEqual(self.restore('restore-wrong-holder',pod_uid='pod-z')['decision'],'stale_holder')
        self.assertEqual(self.restore('restore-wrong-case',conflict='conflict-x')['decision'],'unknown_conflict')
        self.assertEqual(self.restore('restore-ok')['decision'],'original_restored')
        self.assertEqual(len(self.active()),1)

    def test_replacement_during_pending_case_survives_late_clarification(self):
        self.isolate()
        self.assertEqual(self.replace('replace-early')['decision'],'replacement_bound')
        self.assertEqual(one(self.db,"SELECT status FROM address_leases WHERE id='lease-a'")['status'],'isolated')
        self.assertEqual(one(self.db,"SELECT status FROM address_conflicts WHERE id='conflict-a'")['status'],'isolated')
        self.assertEqual(self.resolve()['decision'],'conflict_resolved')
        self.assertEqual(self.restore('restore-late')['decision'],'endpoint_already_active')
        self.assertEqual(self.replace('replace-again',new_lease='lease-c',ip='10.4.0.4')['decision'],'endpoint_already_active')
        active=self.active()
        self.assertEqual(len(active),1)
        self.assertEqual((active[0]['id'],active[0]['ip'],active[0]['pod_uid'],active[0]['node_uid']),('lease-b','10.4.0.3','pod-a','node-a'))
        self.assertEqual(self.renew('renew-replacement','lease-b',1)['decision'],'renewed')
        self.assertEqual(self.renew('renew-isolated-original','lease-a',2)['decision'],'stale_holder')
        permits=many(self.db,"SELECT * FROM recovery_permits WHERE status='activated'")
        self.assertEqual(len(permits),1)
        self.assertEqual(permits[0]['mode'],'replacement')

    def test_restore_first_blocks_later_replacement(self):
        self.isolate()
        self.resolve()
        self.assertEqual(self.restore('restore-first')['decision'],'original_restored')
        self.assertEqual(self.replace('replace-late')['decision'],'lease_not_isolated')
        active=self.active()
        self.assertEqual(len(active),1)
        self.assertEqual(active[0]['ip'],'10.4.0.2')

    def test_unclarified_orphan_not_swept_by_permit(self):
        self.isolate()
        self.resolve()
        act(self.db,'reclaim',{'event_id':'orphan-report','op':'orphan','id':'orphan-a','ip':'10.4.0.2','source':'agent-x'})
        self.assertEqual(self.restore('restore-with-orphan')['decision'],'orphan_remains')
        self.assertEqual(one(self.db,"SELECT status FROM orphan_addresses WHERE id='orphan-a'")['status'],'isolated')
        self.assertIsNone(one(self.db,'SELECT 1 FROM recovery_permits'))
        act(self.db,'reclaim',{'event_id':'orphan-clear','op':'resolve_orphan','id':'orphan-a','independent_receipt':'network-clear'})
        self.assertEqual(self.restore('restore-after-clear')['decision'],'original_restored')

    def test_replacement_address_must_stay_inside_original_tenant_claim(self):
        self.isolate()
        self.assertEqual(self.replace('replace-outside',ip='10.9.0.3')['decision'],'address_outside_current_pool')
        self.assertEqual(self.replace('replace-occupied',ip='10.4.0.2')['decision'],'address_occupied')
        self.assertEqual(self.replace('replace-stale-pool',pool_generation=9)['decision'],'address_outside_current_pool')
        self.assertEqual(self.replace('replace-expired-claim',at=101)['decision'],'source_unavailable')
        act(self.db,'reclaim',{'event_id':'orphan-other','op':'orphan','id':'orphan-b','ip':'10.4.0.4','source':'agent-y'})
        self.assertEqual(self.replace('replace-orphaned',ip='10.4.0.4')['decision'],'orphan_address_blocked')
        self.assertEqual(self.replace('replace-ok')['decision'],'replacement_bound')

    def test_replacement_counts_isolated_lease_against_claim_capacity(self):
        self.run_fixture('pool_normal');self.run_fixture('lease_normal')
        act(self.db,'lease',{'event_id':'issue-extra','op':'issue','id':'lease-x','pool':'pool-a','claim':'claim-a','ip':'10.4.0.3','pod_uid':'pod-x','node_uid':'node-x','pool_generation':1,'at':25})
        act(self.db,'lease',{'event_id':'conflict-report','op':'conflict','id':'conflict-a','ip':'10.4.0.2','other_pod':'pod-z'})
        self.assertEqual(self.replace('replace-full',ip='10.4.0.4')['decision'],'claim_full')

    def test_recovery_events_replay_deterministically(self):
        self.isolate()
        self.replace('replace-first')
        self.resolve()
        self.assertEqual(self.restore('restore-blocked')['decision'],'endpoint_already_active')
        result=replay(str(self.path))
        self.assertTrue(result['verified'],result)
