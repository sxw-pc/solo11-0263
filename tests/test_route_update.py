import json
import unittest
from pathlib import Path

from ipleasecourt.store import act, connect, one, replay

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'fixtures' / 'route_loop_transition.json'

OLD_ROUTE = ['l-AB', 'l-BC', 'l-CD']          # A -> B -> C -> D
NEW_ROUTE = ['l-AC', 'l-CB', 'l-BD']          # A -> C -> B -> D
INTERMEDIATE = ['node-B', 'node-C']


class RouteCutoverTest(unittest.TestCase):
    def setUp(self):
        self.db = connect(':memory:')
        self._seq = 0
        self.steps = json.loads(FIXTURE.read_text(encoding='utf-8'))
        self._by_id = {step['event_id']: step for step in self.steps}
        # Foundation: pools, leases, links, the fixed fabric flow.
        for step in self.steps[:13]:
            act(self.db, step['subsystem'] if 'subsystem' in step else self._subsystem(step), step)

    @staticmethod
    def _subsystem(step):
        return {'publish': 'pool', 'claim': 'pool', 'issue': 'lease', 'fence': 'lease'}.get(
            step['op'], 'fabric')

    def tearDown(self):
        self.db.close()

    def prepare(self, update_id, mode):
        self._seq += 1
        return act(self.db, 'route_update', {
            'event_id': f'prep-{update_id}-{self._seq}', 'op': 'prepare', 'id': update_id,
            'flow': 'flow-rl', 'mode': mode, 'new_route': NEW_ROUTE})

    def inject(self, update_id, packet_id, tag):
        self._seq += 1
        return act(self.db, 'route_update', {
            'event_id': f'inj-{update_id}-{packet_id}-{self._seq}', 'op': 'inject',
            'update_id': update_id, 'packet_id': packet_id, 'tag': tag})

    def forward(self, update_id, packet_id, suffix=''):
        self._seq += 1
        return act(self.db, 'route_update', {
            'event_id': f'fwd-{update_id}-{packet_id}-{self._seq}',
            'op': 'forward', 'update_id': update_id, 'packet_id': packet_id})

    def inflight(self, update_id):
        self._seq += 1
        return act(self.db, 'route_update', {
            'event_id': f'info-{update_id}-{self._seq}', 'op': 'inflight',
            'update_id': update_id})

    def trace_of(self, update_id, packet_id):
        report = self.inflight(update_id)
        packet = next(p for p in report['packets'] if p['packet_id'] == packet_id)
        return packet

    def deliver(self, update_id, packet_id, hops=3):
        decisions = []
        for i in range(hops):
            decisions.append(self.forward(update_id, packet_id, str(i))['decision'])
        return decisions

    def confirm_delivery(self, update_id, packet_id, suffix='final'):
        return self.forward(update_id, packet_id, suffix)

    # ------------------------------------------------------------------ basics

    def test_fixture_routes_are_the_loop_trap_topology(self):
        flow = one(self.db, "SELECT route FROM flow_routes WHERE id='flow-rl'")
        self.assertEqual(json.loads(flow['route']), OLD_ROUTE)
        # Old and new share endpoints but reorder the middle nodes.
        result = self.prepare('cut-prep', 'dual')
        self.assertEqual(result['decision'], 'cutover_prepared')
        self.assertEqual(result['old_nodes'], ['node-A', 'node-B', 'node-C', 'node-D'])
        self.assertEqual(result['new_nodes'], ['node-A', 'node-C', 'node-B', 'node-D'])
        self.assertEqual(result['forwarding_nodes'], INTERMEDIATE)

    def test_prepare_rejects_unknown_flow_and_changed_endpoints(self):
        bad = act(self.db, 'route_update', {
            'event_id': 'no-flow', 'op': 'prepare', 'id': 'x1', 'flow': 'missing',
            'mode': 'dual', 'new_route': NEW_ROUTE})
        self.assertEqual(bad['decision'], 'fabric_flow_unavailable')
        bad_mode = self.prepare('x2', 'triple')
        self.assertEqual(bad_mode['decision'], 'invalid_cutover_mode')

    # ------------------------------------------- install receipt vs. effective

    def test_install_receipt_does_not_make_table_effective(self):
        self.prepare('cut-w', 'dual')
        receipt = act(self.db, 'route_update', {
            'event_id': 'w-install', 'op': 'install', 'update_id': 'cut-w',
            'node': 'node-B', 'tag': 'new'})
        self.assertEqual(receipt['decision'], 'table_install_receipt_recorded')
        self.assertFalse(receipt['effective'])
        # A witness without install is rejected; here install exists but a
        # finish before any witness still reports the per-node gap.
        gap = act(self.db, 'route_update', {
            'event_id': 'w-finish-early', 'op': 'finish', 'update_id': 'cut-w'})
        self.assertEqual(gap['decision'], 'effective_witness_gap')
        self.assertEqual({(m['node'], m['tag']) for m in gap['missing']},
                         {('node-B', 'new'), ('node-C', 'new')})

    def test_witness_without_install_is_rejected_and_late_duplicate_is_idempotent(self):
        self.prepare('cut-late', 'dual')
        ghost = act(self.db, 'route_update', {
            'event_id': 'ghost-witness', 'op': 'witness', 'update_id': 'cut-late',
            'node': 'node-C', 'tag': 'new'})
        self.assertEqual(ghost['decision'], 'witness_without_install')
        # Retried install + witness after an interruption stay idempotent.
        first = act(self.db, 'route_update', {
            'event_id': 'late-install', 'op': 'install', 'update_id': 'cut-late',
            'node': 'node-C', 'tag': 'new'})
        self.assertEqual(first['decision'], 'table_install_receipt_recorded')
        retry = act(self.db, 'route_update', {
            'event_id': 'late-install-retry', 'op': 'install', 'update_id': 'cut-late',
            'node': 'node-C', 'tag': 'new'})
        self.assertEqual(retry['decision'], 'install_receipt_already_recorded')
        witness = act(self.db, 'route_update', {
            'event_id': 'late-witness', 'op': 'witness', 'update_id': 'cut-late',
            'node': 'node-C', 'tag': 'new'})
        self.assertTrue(witness['effective'])
        witness_retry = act(self.db, 'route_update', {
            'event_id': 'late-witness-retry', 'op': 'witness', 'update_id': 'cut-late',
            'node': 'node-C', 'tag': 'new'})
        self.assertEqual(witness_retry['decision'], 'table_effective_witness_recorded')

    # ------------------------------------------- dual mode: per-node packet checks

    def test_dual_mode_old_packet_keeps_old_route_through_half_switched_fabric(self):
        self.prepare('cut-d1', 'dual')
        # Only node-B has the new table effective; node-C has not switched.
        act(self.db, 'route_update', {'event_id': 'd1-bi', 'op': 'install',
                                      'update_id': 'cut-d1', 'node': 'node-B', 'tag': 'new'})
        act(self.db, 'route_update', {'event_id': 'd1-bw', 'op': 'witness',
                                      'update_id': 'cut-d1', 'node': 'node-B', 'tag': 'new'})
        self.assertEqual(self.inject('cut-d1', 'old1', 'old')['decision'], 'packet_forwarded')
        hops = self.deliver('cut-d1', 'old1')
        self.assertEqual(hops, ['packet_forwarded', 'packet_forwarded', 'packet_delivered'])
        self.assertEqual(self.confirm_delivery('cut-d1', 'old1')['decision'],
                         'packet_already_delivered')
        packet = self.trace_of('cut-d1', 'old1')
        # The old-tag packet must walk the original route even though B's
        # new slot is already live: no wrap-back through A or C->B reorder.
        self.assertEqual(packet['trace'],
                         ['node-A', 'node-B', 'node-C', 'node-D', 'node-D:delivered'])

    def test_dual_mode_new_packet_holds_at_unswitched_node_then_completes(self):
        self.prepare('cut-d2', 'dual')
        # Switch node-B only, then inject a new-tag packet.  At node-C the
        # new table is not effective yet: it must hold, not use the old table
        # (using it would send C->D and silently take the old path).
        act(self.db, 'route_update', {'event_id': 'd2-bi', 'op': 'install',
                                      'update_id': 'cut-d2', 'node': 'node-B', 'tag': 'new'})
        act(self.db, 'route_update', {'event_id': 'd2-bw', 'op': 'witness',
                                      'update_id': 'cut-d2', 'node': 'node-B', 'tag': 'new'})
        self.assertEqual(self.inject('cut-d2', 'new1', 'new')['at_node'], 'node-C')
        blocked = self.forward('cut-d2', 'new1', 'hold')
        self.assertEqual(blocked['decision'], 'packet_held_awaiting_table')
        self.assertEqual(blocked['at_node'], 'node-C')
        # Interrupt and retry the node-C installation (duplicate B receipts
        # prove the half-done state is recoverable).
        act(self.db, 'route_update', {'event_id': 'd2-bw-again', 'op': 'witness',
                                      'update_id': 'cut-d2', 'node': 'node-B', 'tag': 'new'})
        act(self.db, 'route_update', {'event_id': 'd2-ci', 'op': 'install',
                                      'update_id': 'cut-d2', 'node': 'node-C', 'tag': 'new'})
        still_held = self.forward('cut-d2', 'new1', 'still-hold')
        self.assertEqual(still_held['decision'], 'packet_held_awaiting_table')
        act(self.db, 'route_update', {'event_id': 'd2-cw', 'op': 'witness',
                                      'update_id': 'cut-d2', 'node': 'node-C', 'tag': 'new'})
        hops = self.deliver('cut-d2', 'new1')
        self.assertEqual(hops, ['packet_forwarded', 'packet_forwarded', 'packet_delivered'])
        packet = self.trace_of('cut-d2', 'new1')
        self.assertEqual(packet['trace'],
                         ['node-A', 'node-C', 'node-B', 'node-D', 'node-D:delivered'])
        finish = act(self.db, 'route_update', {
            'event_id': 'd2-finish', 'op': 'finish', 'update_id': 'cut-d2'})
        self.assertEqual(finish['decision'], 'cutover_complete')

    def test_dual_mode_old_and_new_packets_coexist_without_loop_or_revisit(self):
        self.prepare('cut-d3', 'dual')
        for node in INTERMEDIATE:
            act(self.db, 'route_update', {'event_id': f'd3-i-{node}', 'op': 'install',
                                          'update_id': 'cut-d3', 'node': node, 'tag': 'new'})
            act(self.db, 'route_update', {'event_id': f'd3-w-{node}', 'op': 'witness',
                                          'update_id': 'cut-d3', 'node': node, 'tag': 'new'})
        self.inject('cut-d3', 'o', 'old')
        self.inject('cut-d3', 'n', 'new')
        self.deliver('cut-d3', 'o')
        self.deliver('cut-d3', 'n')
        old_packet = self.trace_of('cut-d3', 'o')
        new_packet = self.trace_of('cut-d3', 'n')
        self.assertEqual(old_packet['trace'],
                         ['node-A', 'node-B', 'node-C', 'node-D', 'node-D:delivered'])
        self.assertEqual(new_packet['trace'],
                         ['node-A', 'node-C', 'node-B', 'node-D', 'node-D:delivered'])
        for packet in (old_packet, new_packet):
            nodes = [step.replace(':delivered', '') for step in packet['trace']]
            self.assertEqual(len(nodes), len(set(nodes)) + 1, f'loop/revisit in {nodes}')
            self.assertEqual(nodes[-1], nodes[-2])
            self.assertTrue(packet['delivered'])
            self.assertFalse(packet['held'])

    def test_mid_install_interruption_never_reports_success_without_witnesses(self):
        self.prepare('cut-d4', 'dual')
        # Simulate a crash after node-B's write landed but before its witness,
        # and node-C entirely untouched.
        act(self.db, 'route_update', {'event_id': 'd4-bi', 'op': 'install',
                                      'update_id': 'cut-d4', 'node': 'node-B', 'tag': 'new'})
        gap = act(self.db, 'route_update', {
            'event_id': 'd4-finish', 'op': 'finish', 'update_id': 'cut-d4'})
        self.assertEqual(gap['decision'], 'effective_witness_gap')
        self.assertNotEqual(gap.get('missing'), [])
        self.assertEqual(one(self.db,
                             "SELECT status FROM route_updates WHERE id='cut-d4'")['status'],
                         'prepared')

    def test_mixed_tables_never_bounce_a_packet_back_through_a_visited_node(self):
        # The dangerous ordering: node-C switches first.  If a node were to
        # ignore the packet tag and use whichever table is newest, an old
        # packet arriving at C would be sent C->B (new table), while B still
        # forwards old traffic B->C: an immediate B<->C loop.
        self.prepare('cut-loop', 'dual')
        act(self.db, 'route_update', {'event_id': 'loop-ci', 'op': 'install',
                                      'update_id': 'cut-loop', 'node': 'node-C', 'tag': 'new'})
        act(self.db, 'route_update', {'event_id': 'loop-cw', 'op': 'witness',
                                      'update_id': 'cut-loop', 'node': 'node-C', 'tag': 'new'})
        self.inject('cut-loop', 'oldie', 'old')
        # A->B (B still old), then B->C, then at C the old-tag lookup must
        # pick the OLD entry C->D, never the newly effective C->B entry.
        hop_b = self.forward('cut-loop', 'oldie', 'to-b')
        hop_c = self.forward('cut-loop', 'oldie', 'to-c')
        hop_d = self.forward('cut-loop', 'oldie', 'to-d')
        self.assertEqual([hop_b['at_node'], hop_c['at_node']],
                         ['node-C', 'node-D'])
        self.assertEqual(hop_d['decision'], 'packet_delivered')
        packet = self.trace_of('cut-loop', 'oldie')
        self.assertEqual(packet['trace'],
                         ['node-A', 'node-B', 'node-C', 'node-D', 'node-D:delivered'])
        self.assertNotIn('node-B', packet['trace'][2:])

    def test_held_packets_resume_inside_waiting_window_without_misdelivery(self):
        # New-tag packet held at C; while it waits, an old-tag packet for the
        # same flow crosses the same node.  Neither may be misdelivered.
        self.prepare('cut-window', 'dual')
        act(self.db, 'route_update', {'event_id': 'win-bi', 'op': 'install',
                                      'update_id': 'cut-window', 'node': 'node-B', 'tag': 'new'})
        act(self.db, 'route_update', {'event_id': 'win-bw', 'op': 'witness',
                                      'update_id': 'cut-window', 'node': 'node-B', 'tag': 'new'})
        self.inject('cut-window', 'new-wait', 'new')
        held = self.forward('cut-window', 'new-wait', 'held')
        self.assertEqual(held['decision'], 'packet_held_awaiting_table')
        # Old traffic flows through the waiting node untouched.
        self.inject('cut-window', 'old-pass', 'old')
        self.deliver('cut-window', 'old-pass')
        self.assertEqual(self.trace_of('cut-window', 'old-pass')['trace'],
                         ['node-A', 'node-B', 'node-C', 'node-D', 'node-D:delivered'])
        # The waiting packet is still parked at C, not spuriously delivered.
        parked = self.trace_of('cut-window', 'new-wait')
        self.assertTrue(parked['held'])
        self.assertFalse(parked['delivered'])
        self.assertEqual(parked['at_node'], 'node-C')
        # Closing the window lets the held packet continue on the new route:
        # it already reached C, so two hops remain (C->B, B->D delivered).
        act(self.db, 'route_update', {'event_id': 'win-ci', 'op': 'install',
                                      'update_id': 'cut-window', 'node': 'node-C', 'tag': 'new'})
        act(self.db, 'route_update', {'event_id': 'win-cw', 'op': 'witness',
                                      'update_id': 'cut-window', 'node': 'node-C', 'tag': 'new'})
        remaining = self.deliver('cut-window', 'new-wait', hops=3)
        self.assertEqual(remaining,
                         ['packet_forwarded', 'packet_forwarded', 'packet_delivered'])
        self.assertEqual(self.trace_of('cut-window', 'new-wait')['trace'],
                         ['node-A', 'node-C', 'node-B', 'node-D', 'node-D:delivered'])

    # ------------------------------------------- single mode: pause + real drain

    def test_single_mode_requires_pause_before_install_and_drain_before_cutover(self):
        self.prepare('cut-s1', 'single')
        eager = act(self.db, 'route_update', {
            'event_id': 's1-eager-install', 'op': 'install', 'update_id': 'cut-s1',
            'node': 'node-B', 'tag': 'new'})
        self.assertEqual(eager['decision'], 'pause_required_before_install')
        early_finish = act(self.db, 'route_update', {
            'event_id': 's1-early-finish', 'op': 'finish', 'update_id': 'cut-s1'})
        self.assertEqual(early_finish['decision'], 'drain_required_before_cutover')
        pause = act(self.db, 'route_update', {
            'event_id': 's1-pause', 'op': 'pause', 'update_id': 'cut-s1'})
        self.assertEqual(pause['decision'], 'ingress_paused')
        # New traffic is refused while the single slot is mid-handover.
        held_ingress = self.inject('cut-s1', 'newbie', 'new')
        self.assertEqual(held_ingress['decision'], 'ingress_paused_hold')

    def test_single_mode_drains_real_undelivered_packets_then_cuts_over(self):
        self.prepare('cut-s2', 'single')
        self.assertEqual(self.inject('cut-s2', 'inflight1', 'old')['at_node'], 'node-B')
        pause = act(self.db, 'route_update', {
            'event_id': 's2-pause', 'op': 'pause', 'update_id': 'cut-s2'})
        self.assertEqual(pause['decision'], 'ingress_paused')
        blocked_drain = act(self.db, 'route_update', {
            'event_id': 's2-drain-blocked', 'op': 'drain', 'update_id': 'cut-s2'})
        self.assertEqual(blocked_drain['decision'], 'packets_remain')
        self.assertEqual(blocked_drain['in_flight'], ['inflight1'])
        # Genuinely deliver every in-flight packet on the old table.
        hops = self.deliver('cut-s2', 'inflight1', hops=2)
        self.assertEqual(hops, ['packet_forwarded', 'packet_forwarded'])
        self.assertEqual(self.confirm_delivery('cut-s2', 'inflight1')['decision'],
                         'packet_delivered')
        drained = act(self.db, 'route_update', {
            'event_id': 's2-drain', 'op': 'drain', 'update_id': 'cut-s2'})
        self.assertEqual(drained['decision'], 'ingress_drained')
        # Installs after the pause are now accepted; witnesses still required.
        for node in INTERMEDIATE:
            self.assertEqual(act(self.db, 'route_update', {
                'event_id': f's2-i-{node}', 'op': 'install', 'update_id': 'cut-s2',
                'node': node, 'tag': 'new'})['decision'],
                'table_install_receipt_recorded')
        gap = act(self.db, 'route_update', {
            'event_id': 's2-finish-gap', 'op': 'finish', 'update_id': 'cut-s2'})
        self.assertEqual(gap['decision'], 'effective_witness_gap')
        for node in INTERMEDIATE:
            act(self.db, 'route_update', {'event_id': f's2-w-{node}', 'op': 'witness',
                                          'update_id': 'cut-s2', 'node': node, 'tag': 'new'})
        finish = act(self.db, 'route_update', {
            'event_id': 's2-finish', 'op': 'finish', 'update_id': 'cut-s2'})
        self.assertEqual(finish['decision'], 'cutover_complete')
        # After cutover a new packet takes the new route end to end.
        injected = self.inject('cut-s2', 'after', 'new')
        self.assertEqual(injected['decision'], 'packet_forwarded')
        self.assertEqual(injected['at_node'], 'node-C')
        self.deliver('cut-s2', 'after')
        self.assertEqual(self.trace_of('cut-s2', 'after')['trace'],
                         ['node-A', 'node-C', 'node-B', 'node-D', 'node-D:delivered'])

    def test_single_mode_interrupted_install_resumes_and_keeps_old_path_live(self):
        self.prepare('cut-s3', 'single')
        self.inject('cut-s3', 'oldie', 'old')
        act(self.db, 'route_update', {'event_id': 's3-pause', 'op': 'pause',
                                      'update_id': 'cut-s3'})
        self.deliver('cut-s3', 'oldie')
        act(self.db, 'route_update', {'event_id': 's3-drain', 'op': 'drain',
                                      'update_id': 'cut-s3'})
        # Node-B installed + witnessed; node-C install interrupted (no witness).
        act(self.db, 'route_update', {'event_id': 's3-bi', 'op': 'install',
                                      'update_id': 'cut-s3', 'node': 'node-B', 'tag': 'new'})
        act(self.db, 'route_update', {'event_id': 's3-bw', 'op': 'witness',
                                      'update_id': 'cut-s3', 'node': 'node-B', 'tag': 'new'})
        act(self.db, 'route_update', {'event_id': 's3-ci', 'op': 'install',
                                      'update_id': 'cut-s3', 'node': 'node-C', 'tag': 'new'})
        gap = act(self.db, 'route_update', {
            'event_id': 's3-finish-gap', 'op': 'finish', 'update_id': 'cut-s3'})
        self.assertEqual(gap['decision'], 'effective_witness_gap')
        # Retry node-C after the interruption: its write receipt survived,
        # so the duplicate is acknowledged and only the witness remains.
        self.assertEqual(act(self.db, 'route_update', {
            'event_id': 's3-ci-retry', 'op': 'install',
            'update_id': 'cut-s3', 'node': 'node-C', 'tag': 'new'})['decision'],
            'install_receipt_already_recorded')
        act(self.db, 'route_update', {'event_id': 's3-cw', 'op': 'witness',
                                      'update_id': 'cut-s3', 'node': 'node-C', 'tag': 'new'})
        self.assertEqual(act(self.db, 'route_update', {
            'event_id': 's3-finish', 'op': 'finish',
            'update_id': 'cut-s3'})['decision'], 'cutover_complete')

    # ------------------------------------------- lease fencing / generation guard

    def test_fenced_destination_blocks_old_label_from_another_lease_generation(self):
        self.prepare('cut-fence', 'dual')
        for node in INTERMEDIATE:
            act(self.db, 'route_update', {'event_id': f'fe-i-{node}', 'op': 'install',
                                          'update_id': 'cut-fence', 'node': node, 'tag': 'new'})
            act(self.db, 'route_update', {'event_id': f'fe-w-{node}', 'op': 'witness',
                                          'update_id': 'cut-fence', 'node': node, 'tag': 'new'})
        self.inject('cut-fence', 'doomed', 'old')
        # Fence the destination endpoint to a newer generation (workload move).
        fence = act(self.db, 'lease', {
            'event_id': 'fe-fence', 'op': 'fence', 'id': 'fence-rl-d',
            'lease': 'lease-rl-d', 'generation': 2, 'at': 90})
        self.assertEqual(fence['decision'], 'holder_fenced')
        # The very next hop must be refused: the old label cannot enter the
        # endpoint now owned by another lease generation.
        final = self.forward('cut-fence', 'doomed', 'fenced-hop')
        self.assertEqual(final['decision'], 'endpoint_generation_fenced')
        packet = self.trace_of('cut-fence', 'doomed')
        self.assertFalse(packet['delivered'])
        self.assertTrue(packet['held'])
        self.assertEqual(packet['at_node'], 'node-B')
        # Ingress also rejects new packets once the generation moved.
        self.assertEqual(self.inject('cut-fence', 'rejected', 'new')['decision'],
                         'fabric_endpoint_unavailable')

    # ------------------------------------------- fixture and replay integrity

    def test_full_fixture_replays_to_identical_state(self):
        db = connect(':memory:')
        results = []
        for step in self.steps:
            subsystem = self._subsystem(step) if step['op'] in (
                'publish', 'claim', 'issue', 'fence') else (
                'route_update' if step['op'] in {
                    'prepare', 'install', 'witness', 'pause', 'inject', 'forward',
                    'drain', 'finish', 'inflight', 'advance_all'} else 'fabric')
            results.append(act(db, subsystem, step)['decision'])
        # Spot-check the key decisions embedded in the fixture narrative.
        self.assertIn('cutover_prepared', results)
        self.assertIn('cutover_complete', results)
        self.assertIn('packets_remain', results)
        self.assertIn('ingress_paused_hold', results)
        verification = replay_db(db)
        self.assertTrue(verification['verified'], verification)
        db.close()


def replay_db(db):
    # Reuse store.replay against a serialized copy by backing the in-memory
    # database up through the backup API into a temp file.
    import tempfile
    from ipleasecourt.store import replay
    with tempfile.NamedTemporaryFile(suffix='.sqlite') as handle:
        target = connect(handle.name)
        db.backup(target)
        target.close()
        return replay(handle.name)


if __name__ == '__main__':
    unittest.main()
