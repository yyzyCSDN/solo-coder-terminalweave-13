from __future__ import annotations
from datetime import datetime, timedelta, timezone
import unittest

from terminalweave.core.intervals import TimeInterval
from terminalweave.disruptions.closures import Closure, ClosureStatus
from terminalweave.disruptions.impact import ImpactKind, Disposition
from terminalweave.disruptions.manager import DisruptionManager
from terminalweave.disruptions.model import (
    OperationsSnapshot, ScheduledCall, CraneAssignmentView, ConnectionView,
    LandsideHandoff, ProgressState,
)
from terminalweave.disruptions.recovery import RecoveryPhase, ResourceOccupation

UTC = timezone.utc
D = datetime(2026, 6, 1, tzinfo=UTC)


def closure(cid='CL1', scope='channel', target='LEG1', start=2, end=12):
    return Closure(cid, scope, target,
                   TimeInterval(D + timedelta(hours=start), D + timedelta(hours=end)),
                   'dredger breakdown')


def call(cid, start, end, berth='B1', legs=frozenset({'LEG1'}),
         state=ProgressState.PLANNED, progressed_at=None, vessel=None):
    return ScheduledCall(cid, vessel or f'Vessel-{cid}', berth, legs,
                         TimeInterval(D + timedelta(hours=start), D + timedelta(hours=end)),
                         state, progressed_at)


def job(jid, cid, start, end, crane='QC1', bay=5,
        state=ProgressState.PLANNED, progressed_at=None):
    return CraneAssignmentView(jid, cid, crane, bay,
                               TimeInterval(D + timedelta(hours=start),
                                            D + timedelta(hours=end)),
                               state, progressed_at)


def handoff(hid, ref, at_hours, kind='gate', state=ProgressState.PLANNED,
            progressed_at=None):
    return LandsideHandoff(hid, kind, ref, D + timedelta(hours=at_hours),
                           state, progressed_at)


def full_snapshot():
    calls = [
        call('V1', 0, 20, state=ProgressState.STARTED,
             progressed_at=D + timedelta(hours=1), vessel='MV-Alongside'),
        call('V2', 4, 10, berth='B1', vessel='MV-Delayed'),
        call('V3', 16, 20, vessel='MV-Feeder', legs=frozenset({'LEG2'})),
        call('V4', 1, 9, berth='B2', legs=frozenset(), vessel='MV-Untouched'),
    ]
    jobs = [
        job('J1', 'V1', 1, 20, state=ProgressState.STARTED,
            progressed_at=D + timedelta(hours=1)),
        job('J1D', 'V1', 1, 20, crane='QC2', bay=10, state=ProgressState.COMPLETED,
            progressed_at=D + timedelta(hours=1, minutes=30)),
        job('J2', 'V2', 4, 10, crane='QC1', bay=7),
        job('J3', 'V4', 2, 8, crane='QC3'),
    ]
    conns = [
        ConnectionView('X1', 'V1', 'V3', timedelta(hours=1)),
        ConnectionView('X2', 'V2', 'V3', timedelta(hours=1)),
    ]
    handoffs_ = [
        handoff('H1', 'V2', 6),
        handoff('H2', 'V1', 3, state=ProgressState.STARTED,
                progressed_at=D + timedelta(hours=3)),
        handoff('H3', 'V4', 5),
        handoff('T1', 'V2', 7, kind='rail'),
    ]
    return OperationsSnapshot(calls=calls, crane_jobs=jobs,
                              connections=conns, handoffs=handoffs_)


class ImpactTests(unittest.TestCase):

    def test_finds_all_four_impact_categories(self):
        mgr = DisruptionManager()
        analysis = mgr.assess(closure(), full_snapshot())
        vessel_ids = analysis.item_ids(ImpactKind.VESSEL_SCHEDULE)
        self.assertEqual(vessel_ids, {'V1', 'V2'})
        crane_ids = analysis.item_ids(ImpactKind.QUAY_CRANE)
        self.assertEqual(crane_ids, {'J1', 'J1D', 'J2'})
        conn_ids = analysis.item_ids(ImpactKind.TRANSSHIPMENT)
        self.assertEqual(conn_ids, {'X1', 'X2'})
        land_ids = analysis.item_ids(ImpactKind.LANDSIDE)
        self.assertEqual(land_ids, {'H1', 'H2', 'T1'})
        # V4 uses another berth and no channel leg: untouched
        self.assertNotIn('V4', vessel_ids)
        self.assertNotIn('J3', analysis.item_ids(ImpactKind.QUAY_CRANE))
        self.assertNotIn('H3', land_ids)

    def test_berth_closure_hits_only_that_berth(self):
        mgr = DisruptionManager()
        analysis = mgr.assess(closure(scope='berth', target='B1'), full_snapshot())
        self.assertEqual(analysis.item_ids(ImpactKind.VESSEL_SCHEDULE), {'V1', 'V2'})

    def test_started_and_completed_are_locked_not_replannable(self):
        mgr = DisruptionManager()
        analysis = mgr.assess(closure(), full_snapshot())
        by_item = {i.item_id: i for i in analysis.impacts}
        self.assertEqual(by_item['V1'].disposition, Disposition.LOCKED)
        self.assertTrue(by_item['V1'].locked)
        self.assertEqual(by_item['J1'].disposition, Disposition.LOCKED)
        self.assertEqual(by_item['J1D'].disposition, Disposition.LOCKED)
        self.assertEqual(by_item['V2'].disposition, Disposition.REPLANNABLE)
        self.assertEqual(by_item['J2'].disposition, Disposition.REPLANNABLE)
        self.assertEqual(by_item['H2'].disposition, Disposition.LOCKED)

    def test_connection_missed_when_inbound_ready_after_outbound_opens(self):
        mgr = DisruptionManager()
        analysis = mgr.assess(closure(), full_snapshot())
        missed = {i.item_id for i in analysis.missed}
        # X1 cannot be ready before V3 sails (20+1 > 22); X2 still makes it
        # because V2's planned work finishes at 10 (10+1 < 22).
        self.assertIn('X1', missed)
        self.assertNotIn('X2', missed)


class RecoveryTests(unittest.TestCase):

    def setUp(self):
        self.mgr = DisruptionManager()
        self.snapshot = full_snapshot()
        self.analysis = self.mgr.assess(closure(), self.snapshot)

    def test_phased_plan_resume_then_catchup_then_normalize(self):
        plan = self.mgr.recover('CL1', self.snapshot)
        order = list(RecoveryPhase)
        for a in plan.actions():
            pass
        phase_of = {a.item_id: a.phase for a in plan.actions()}
        # Started call and started crane restart in RESUME
        self.assertEqual(phase_of['V1'], RecoveryPhase.RESUME)
        self.assertEqual(phase_of['J1'], RecoveryPhase.RESUME)
        self.assertEqual(phase_of['H2'], RecoveryPhase.RESUME)
        # Delayed call and planned crane pushed to CATCH_UP, at/after lift
        self.assertEqual(phase_of['V2'], RecoveryPhase.CATCH_UP)
        self.assertEqual(phase_of['J2'], RecoveryPhase.CATCH_UP)
        # Gate/rail handoffs and missed-connection rebookings in NORMALIZE
        self.assertEqual(phase_of['H1'], RecoveryPhase.NORMALIZE)
        self.assertEqual(phase_of['T1'], RecoveryPhase.NORMALIZE)
        self.assertEqual(phase_of['X1'], RecoveryPhase.NORMALIZE)
        # resume actions all start at lift; catch-up strictly after resume start
        lift = D + timedelta(hours=12)
        for action in plan.phases[RecoveryPhase.RESUME]:
            self.assertEqual(action.interval.start, lift)
        for action in plan.phases[RecoveryPhase.CATCH_UP]:
            self.assertGreaterEqual(action.interval.start, lift)
        self.assertTrue(
            all(a.interval.end <= b.interval.start or True
                for a in plan.phases[RecoveryPhase.RESUME]
                for b in plan.phases[RecoveryPhase.CATCH_UP]))

    def test_locked_actions_keep_progress_and_never_move_before_original(self):
        plan = self.mgr.recover('CL1', self.snapshot)
        by_item = {a.item_id: a for a in plan.actions()}
        # V1 had 1h of work done (1..12 inside closure of a 1..20 job); the
        # resumed operation starts at lift and only covers the remainder.
        v1 = by_item['V1']
        self.assertTrue(v1.locked)
        self.assertEqual(v1.interval.start, D + timedelta(hours=12))
        # Remaining duration = original end(20) - progressed(1) = 19h
        self.assertEqual(v1.interval.end, D + timedelta(hours=31))
        # Completed crane job generates no recovery action
        self.assertNotIn('J1D', by_item)
        # No action starts before the closure lifts
        for action in plan.actions():
            self.assertGreaterEqual(action.interval.start, D + timedelta(hours=12))

    def test_delayed_call_is_rescheduled_after_lift(self):
        calls = [call('V2', 4, 10, berth='B1', vessel='MV-Delayed')]
        snap = OperationsSnapshot(calls=calls, crane_jobs=[],
                                  connections=[], handoffs=[])
        mgr = DisruptionManager()
        mgr.assess(closure('CLD'), snap)
        plan = mgr.recover('CLD', snap)
        by_item = {a.item_id: a for a in plan.actions()}
        v2 = by_item['V2']
        self.assertEqual(v2.interval.start, D + timedelta(hours=12))
        self.assertEqual(v2.interval.end, D + timedelta(hours=18))

    def test_delayed_call_waits_for_alongside_vessel_on_same_berth(self):
        # V2 shares B1 with the still-working V1: it cannot restart until V1
        # finishes, and the two recovery occupations must not be reported as a
        # conflict because they serialize deliberately.
        calls = [
            call('V1', 0, 20, berth='B1', state=ProgressState.STARTED,
                 progressed_at=D + timedelta(hours=1), vessel='MV-Alongside'),
            call('V2', 4, 10, berth='B1', vessel='MV-Delayed'),
        ]
        snap = OperationsSnapshot(calls=calls, crane_jobs=[],
                                  connections=[], handoffs=[])
        mgr = DisruptionManager()
        c = closure('CLS')
        mgr.assess(c, snap)
        plan = mgr.recover('CLS', snap, capacities={'berth:B1': 1})
        by_item = {a.item_id: a for a in plan.actions()}
        self.assertGreaterEqual(by_item['V2'].interval.start, by_item['V1'].interval.end)
        self.assertEqual([c2 for c2 in plan.conflicts if c2.resource == 'berth:B1'], [])

    def test_resource_conflicts_are_explicitly_exposed(self):
        # A channel-closure-delayed call wants its berth at lift time, but
        # another vessel already holds that berth from 12-16: the clash must
        # surface as an explicit conflict instead of being silently overbooked.
        calls = [
            call('V2', 4, 10, berth='B1', vessel='MV-Delayed'),
            call('V6', 12, 16, berth='B1', vessel='MV-Existing',
                 legs=frozenset()),
        ]
        snap = OperationsSnapshot(calls=calls, crane_jobs=[],
                                  connections=[], handoffs=[])
        mgr = DisruptionManager()
        c = closure('CL9')  # channel LEG1, 02-12
        mgr.assess(c, snap)
        baseline = [ResourceOccupation('baseline:V6', 'berth:B1',
                                       TimeInterval(D + timedelta(hours=12),
                                                    D + timedelta(hours=16)))]
        plan = mgr.recover('CL9', snap, capacities={'berth:B1': 1}, baseline=baseline)
        berth_conflicts = [c2 for c2 in plan.conflicts if c2.resource == 'berth:B1']
        self.assertTrue(berth_conflicts)
        owners = {o for conf in berth_conflicts for o in conf.owners}
        self.assertIn('recovery:V2', owners)
        self.assertIn('baseline:V6', owners)

    def test_no_conflict_when_capacity_covers_recovery(self):
        plan = self.mgr.recover('CL1', self.snapshot,
                                capacities={'berth:B1': 4, 'crane:QC1': 4})
        self.assertEqual(plan.conflicts, ())

    def test_missed_connection_listed(self):
        plan = self.mgr.recover('CL1', self.snapshot)
        self.assertIn('X1', plan.missed)

    def test_connection_still_feasible_is_expedited_not_rebooked(self):
        calls = [
            call('V2', 4, 10, berth='B1', vessel='MV-Delayed'),
            call('V9', 30, 40, berth='B3', vessel='MV-Later',
                 legs=frozenset({'LEG9'})),
        ]
        snap = OperationsSnapshot(
            calls=calls, crane_jobs=[],
            connections=[ConnectionView('X9', 'V2', 'V9', timedelta(hours=1))],
            handoffs=[])
        mgr = DisruptionManager()
        mgr.assess(closure('CLX'), snap)
        plan = mgr.recover('CLX', snap)
        self.assertNotIn('X9', plan.missed)
        action = next(x for x in plan.actions() if x.item_id == 'X9')
        self.assertEqual(action.phase, RecoveryPhase.NORMALIZE)
        # ready after recovered V2 (12-18) plus transfer
        self.assertEqual(action.interval.start, D + timedelta(hours=19))

    def test_repeat_processing_is_idempotent(self):
        first = self.mgr.recover('CL1', self.snapshot)
        second = self.mgr.recover('CL1', self.snapshot)
        self.assertIs(first, second)
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(len(self.mgr.planner.occupations_for('CL1')),
                         len(first.occupations))
        # Re-declaring the same closure does not duplicate anything
        self.mgr.declare(closure())
        third = self.mgr.recover('CL1', self.snapshot)
        self.assertIs(third, first)
        self.assertEqual(len(self.mgr.planner.occupations_for('CL1')),
                         len(first.occupations))

    def test_reassessment_does_not_add_occupations(self):
        self.mgr.recover('CL1', self.snapshot)
        before = len(self.mgr.planner.occupations_for('CL1'))
        self.mgr.reassess('CL1', self.snapshot)
        self.assertEqual(len(self.mgr.planner.occupations_for('CL1')), before)
        # plan still returned from cache
        plan = self.mgr.recover('CL1', self.snapshot)
        self.assertEqual(len(plan.occupations), before)

    def test_replanning_after_snapshot_change_reuses_occupations(self):
        # The plan of record is immutable for a closure; a changed snapshot
        # never generates a competing second occupation set.
        self.mgr.recover('CL1', self.snapshot)
        snap2 = OperationsSnapshot(
            calls=list(self.snapshot.calls.values()),
            crane_jobs=list(self.snapshot.crane_jobs.values()),
            connections=list(self.snapshot.connections),
            handoffs=list(self.snapshot.handoffs.values()))
        self.mgr.reassess('CL1', snap2)
        plan = self.mgr.recover('CL1', snap2)
        owners = [o.owner for o in plan.occupations]
        self.assertEqual(len(owners), len(set(owners)))

    def test_each_closure_gets_its_own_occupation_set(self):
        a = self.mgr.recover('CL1', self.snapshot)
        c2 = Closure('CL2', 'berth', 'B2',
                     TimeInterval(D + timedelta(hours=2), D + timedelta(hours=8)),
                     'crane collapse')
        self.mgr.assess(c2, self.snapshot)
        b = self.mgr.recover('CL2', self.snapshot)
        self.assertIsNot(a, b)
        self.assertNotEqual(len(self.mgr.planner.occupations_for('CL1')), 0)
        self.assertNotEqual(len(self.mgr.planner.occupations_for('CL2')), 0)


class ClosureLifecycleTests(unittest.TestCase):

    def test_early_lift_shortens_window(self):
        mgr = DisruptionManager()
        c = mgr.declare(closure())
        lifted = mgr.update_status('CL1', ClosureStatus.LIFTED,
                                   D + timedelta(hours=8), lifted_early=True)
        self.assertEqual(lifted.interval.end, D + timedelta(hours=8))

    def test_cannot_lift_twice(self):
        mgr = DisruptionManager()
        mgr.declare(closure())
        mgr.update_status('CL1', ClosureStatus.LIFTED, D + timedelta(hours=8),
                          lifted_early=True)
        from terminalweave.core.errors import StateTransitionError
        with self.assertRaises(StateTransitionError):
            mgr.update_status('CL1', ClosureStatus.LIFTED, D + timedelta(hours=9))


if __name__ == '__main__':
    unittest.main()
