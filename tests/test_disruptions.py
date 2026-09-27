from datetime import datetime, timedelta, timezone

import pytest

from terminalweave.core.errors import (
    StateTransitionError,
    ValidationError,
)
from terminalweave.core.intervals import TimeInterval
from terminalweave.planning.conflicts import ResourceUse
from terminalweave.disruptions import (
    ActionState,
    CraneOperation,
    Disruption,
    DisruptionKind,
    DisruptionRegistry,
    HandoverKind,
    ImpactAnalyzer,
    ImpactSeverity,
    LandsideHandover,
    OperationsSnapshot,
    RecoveryPlanner,
    ScheduleEntry,
    TransshipmentLink,
)
from terminalweave.disruptions.model import (
    PHASE_CATCH_UP,
    PHASE_CONTAINMENT,
    PHASE_NORMALIZATION,
    PHASE_RESUMPTION,
    ACT_REBOOK,
    ACT_HOLD,
    ACT_CHANNEL_TRANSIT,
    ACT_RESUME_CRANE,
)

UTC = timezone.utc
D = datetime(2026, 3, 1, 8, tzinfo=UTC)


def h(hours: float) -> datetime:
    return D + timedelta(hours=hours)


def make_snapshot(**overrides) -> OperationsSnapshot:
    defaults = dict(
        calls=(
            ScheduleEntry(
                'V1', 'B1', TimeInterval(h(0), h(12)),
                inbound_channel_window=TimeInterval(h(-1), h(1)),
                outbound_channel_window=TimeInterval(h(12), h(14)),
            ),
            ScheduleEntry(
                'V2', 'B2', TimeInterval(h(10), h(20)),
                inbound_channel_window=TimeInterval(h(8), h(10)),
            ),
        ),
        crane_operations=(
            CraneOperation('J1', 'V1', 'QC1', TimeInterval(h(1), h(5))),
            CraneOperation('J2', 'V2', 'QC2', TimeInterval(h(11), h(15))),
        ),
        transshipments=(
            TransshipmentLink('C1', 'V1', 'V2', h(3), h(20)),
            TransshipmentLink('C2', 'V1', 'V2', h(2), h(13)),
        ),
        handovers=(
            LandsideHandover('H1', HandoverKind.GATE, h(2), 'V1'),
            LandsideHandover('R1', HandoverKind.RAIL, h(6), None, frozenset({'C1'})),
        ),
        baseline_uses=(),
    )
    defaults.update(overrides)
    return OperationsSnapshot(**defaults)


# ---------------------------------------------------------------------------
# impact identification
# ---------------------------------------------------------------------------

def test_channel_closure_finds_direct_call_and_cascading_dependents():
    impact = ImpactAnalyzer().analyze(
        Disruption('X1', DisruptionKind.CHANNEL, 'MAIN', TimeInterval(h(0), h(6))),
        make_snapshot(),
    )
    assert [c.call_id for c in impact.calls] == ['V1']
    assert impact.calls[0].severity is ImpactSeverity.DIRECT

    cranes = {j.job_id: j for j in impact.crane_operations}
    assert set(cranes) == {'J1'}
    assert cranes['J1'].severity is ImpactSeverity.CASCADING

    trans = {t.container_id: t for t in impact.transshipments}
    assert set(trans) == {'C1', 'C2'}
    assert all(t.severity is ImpactSeverity.DIRECT for t in trans.values())

    hand = {x.handover_id: x for x in impact.handovers}
    assert set(hand) == {'H1', 'R1'}
    assert all(x.severity is ImpactSeverity.CASCADING for x in hand.values())


def test_berth_closure_marks_overlapping_crane_work_direct():
    impact = ImpactAnalyzer().analyze(
        Disruption('X1', DisruptionKind.BERTH, 'B1', TimeInterval(h(2), h(8))),
        make_snapshot(),
    )
    assert {c.call_id for c in impact.calls} == {'V1'}
    cranes = {j.job_id: j.severity for j in impact.crane_operations}
    assert cranes == {'J1': ImpactSeverity.DIRECT}


def test_closure_with_no_touching_windows_has_no_impact():
    impact = ImpactAnalyzer().analyze(
        Disruption('X1', DisruptionKind.CHANNEL, 'MAIN', TimeInterval(h(20), h(22))),
        make_snapshot(),
    )
    assert impact.is_empty


def test_handover_inside_closure_window_is_directly_impacted():
    snap = OperationsSnapshot(
        handovers=(LandsideHandover('H9', HandoverKind.GATE, h(3)),),
    )
    impact = ImpactAnalyzer().analyze(
        Disruption('X1', DisruptionKind.CHANNEL, 'MAIN', TimeInterval(h(0), h(6))),
        snap,
    )
    assert [x.handover_id for x in impact.handovers] == ['H9']
    assert impact.handovers[0].severity is ImpactSeverity.DIRECT


# ---------------------------------------------------------------------------
# phased recovery
# ---------------------------------------------------------------------------

def test_channel_closure_plan_is_staged_containment_to_normalization():
    registry = DisruptionRegistry()
    plan = registry.process(
        Disruption('X1', DisruptionKind.CHANNEL, 'MAIN', TimeInterval(h(0), h(6))),
        make_snapshot(),
    )
    phases = [p.name for p in plan.phases]
    assert phases[0] == PHASE_CONTAINMENT
    assert phases[1] == PHASE_RESUMPTION
    assert phases[-1] == PHASE_CATCH_UP

    by_type = {}
    for action in plan.actions:
        by_type.setdefault(action.action_type, []).append(action)
    assert ACT_HOLD in by_type and ACT_CHANNEL_TRANSIT in by_type
    assert ACT_RESUME_CRANE in by_type
    # Both connections retain enough slack in the fixture, so no rebooking is
    # proposed; rescheduling actions occupy catch-up resources instead.
    assert ACT_REBOOK not in by_type


def test_tight_connection_is_rebooked_without_resource_occupancy():
    snap = make_snapshot(
        transshipments=(
            TransshipmentLink('C-TIGHT', 'V1', 'V2', h(2), h(8)),
        ),
    )
    registry = DisruptionRegistry()
    plan = registry.process(
        Disruption('X1', DisruptionKind.CHANNEL, 'MAIN', TimeInterval(h(0), h(6))),
        snap,
    )
    rebook = next(a for a in plan.actions if a.action_type == ACT_REBOOK)
    assert rebook.target_ref == 'C-TIGHT'
    assert rebook.phase == PHASE_NORMALIZATION
    assert rebook.resource is None and rebook.interval is None
    assert all(u.owner != rebook.action_id for u in plan.reservations())


def test_resource_conflicts_are_exposed_not_silently_rescheduled():
    snap = make_snapshot(
        calls=(
            ScheduleEntry('V1', 'B1', TimeInterval(h(0), h(12)),
                          inbound_channel_window=TimeInterval(h(-1), h(1))),
            ScheduleEntry('V2', 'B1', TimeInterval(h(0), h(12)),
                          inbound_channel_window=TimeInterval(h(-1), h(1))),
        ),
        crane_operations=(),
        transshipments=(),
        handovers=(),
        baseline_uses=(ResourceUse('committed', 'channel:MAIN', TimeInterval(h(3), h(4))),),
    )
    planner = RecoveryPlanner(stagger=timedelta(0))
    registry = DisruptionRegistry(planner=planner)
    plan = registry.process(
        Disruption('X1', DisruptionKind.CHANNEL, 'MAIN', TimeInterval(h(0), h(6))),
        snap,
    )
    owner_sets = [set(c.owners) for c in plan.conflicts]
    assert {'X1:V1:channel_transit', 'X1:V2:channel_transit'} in owner_sets
    assert any({'X1:closure', 'committed'} <= owners for owners in owner_sets)


# ---------------------------------------------------------------------------
# committed work cannot regress
# ---------------------------------------------------------------------------

def test_started_and_completed_actions_cannot_regress():
    registry = DisruptionRegistry()
    plan = registry.process(
        Disruption('X1', DisruptionKind.CHANNEL, 'MAIN', TimeInterval(h(0), h(6))),
        make_snapshot(),
    )
    aid = 'X1:V1:channel_transit'
    registry.update_action('X1', aid, ActionState.STARTED, h(6))
    with pytest.raises(StateTransitionError):
        registry.update_action('X1', aid, ActionState.PROPOSED, h(7))
    with pytest.raises(ValidationError):
        registry.update_action('X1', aid, ActionState.COMPLETED, h(5))
    with pytest.raises(StateTransitionError):
        registry.update_action('X1', aid, ActionState.CANCELLED, h(6.5))
    registry.update_action('X1', aid, ActionState.COMPLETED, h(7))

    plan2 = registry.reprocess('X1', make_snapshot())
    carried = plan2.action(aid)
    assert carried.state is ActionState.COMPLETED
    assert carried.started_at == h(6) and carried.completed_at == h(7)
    assert carried.interval == plan.action(aid).interval


def test_proposed_action_may_be_cancelled_and_then_releases_occupancy():
    registry = DisruptionRegistry()
    plan = registry.process(
        Disruption('X1', DisruptionKind.CHANNEL, 'MAIN', TimeInterval(h(0), h(6))),
        make_snapshot(),
    )
    aid = 'X1:V1:channel_transit'
    registry.update_action('X1', aid, ActionState.CANCELLED, h(1))
    owners = {u.owner for u in plan.reservations()}
    assert aid not in owners


# ---------------------------------------------------------------------------
# idempotency / no second set of occupancy
# ---------------------------------------------------------------------------

def test_reprocessing_same_disruption_keeps_single_action_set():
    registry = DisruptionRegistry()
    dis = Disruption('X1', DisruptionKind.CHANNEL, 'MAIN', TimeInterval(h(0), h(6)))
    snap = make_snapshot()
    first = registry.process(dis, snap)
    second = registry.reprocess('X1', snap)
    third = registry.reprocess('X1', snap)
    assert [a.action_id for a in first.actions] == [a.action_id for a in second.actions]
    assert [a.action_id for a in second.actions] == [a.action_id for a in third.actions]
    assert third.revision == 3
    # Derived reservations: plan holds exactly one occupancy source.
    assert len(third.reservations()) == len({u.owner for u in third.reservations()})
    assert registry.total_reservations() == {
        'channel:MAIN': 1,
        'crane:QC1': 1,
        'gate:lane': 1,
        'rail:interchange': 1,
        'yard:transfer': 1,
    }


def test_reprocessing_changed_closure_terms_is_rejected():
    registry = DisruptionRegistry()
    snap = make_snapshot()
    registry.process(Disruption('X1', DisruptionKind.CHANNEL, 'MAIN', TimeInterval(h(0), h(6))), snap)
    with pytest.raises(ValidationError):
        registry.process(Disruption('X1', DisruptionKind.CHANNEL, 'MAIN', TimeInterval(h(0), h(5))), snap)


def test_distinct_disruptions_are_registered_separately():
    registry = DisruptionRegistry()
    snap = make_snapshot()
    registry.process(Disruption('X1', DisruptionKind.CHANNEL, 'MAIN', TimeInterval(h(0), h(6))), snap)
    registry.process(Disruption('X2', DisruptionKind.BERTH, 'B1', TimeInterval(h(0), h(6))), snap)
    assert set(registry.disruption_ids) == {'X1', 'X2'}
    plan1 = registry.plan('X1')
    assert all(a.action_id.startswith('X1:') for a in plan1.actions)
