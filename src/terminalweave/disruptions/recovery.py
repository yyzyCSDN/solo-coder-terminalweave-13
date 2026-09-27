from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from terminalweave.core.errors import ValidationError
from terminalweave.core.intervals import TimeInterval
from .closures import Closure
from .impact import Disposition, Impact, ImpactAnalysis, ImpactKind
from .model import OperationsSnapshot, ProgressState


class RecoveryPhase(str, Enum):
    RESUME = 'resume'          # reopen channel/berth, restart in-flight work
    CATCH_UP = 'catch_up'      # queue clearance, delayed calls and crane work
    NORMALIZE = 'normalize'    # transshipment and landside handoffs rebooked


@dataclass(frozen=True)
class RecoveryAction:
    action_id: str
    phase: RecoveryPhase
    item_id: str
    kind: ImpactKind
    interval: TimeInterval
    description: str
    resource: str | None = None
    locked: bool = False


@dataclass(frozen=True)
class RecoveryConflict:
    resource: str
    interval: TimeInterval
    owners: tuple[str, ...]
    excess: int


@dataclass(frozen=True)
class ResourceOccupation:
    """A planned resource use tagged with the closure plan that created it."""
    owner: str
    resource: str
    interval: TimeInterval
    quantity: int = 1


@dataclass(frozen=True)
class RecoveryPlan:
    closure_id: str
    lift_at: datetime
    phases: dict[RecoveryPhase, tuple[RecoveryAction, ...]]
    conflicts: tuple[RecoveryConflict, ...]
    occupations: tuple[ResourceOccupation, ...]
    missed: tuple[str, ...]
    fingerprint: tuple

    def actions(self) -> tuple[RecoveryAction, ...]:
        return tuple(sorted(
            (a for actions in self.phases.values() for a in actions),
            key=lambda a: (list(RecoveryPhase).index(a.phase), a.interval.start, a.item_id, a.action_id),
        ))


class RecoveryPlanner:
    """Builds phased recovery plans from an impact analysis.

    Idempotency: ``plan`` called twice for one closure returns the identical
    plan and never creates a second set of resource occupations.
    """

    def __init__(self, increment: timedelta = timedelta(minutes=5)):
        if increment.total_seconds() <= 0:
            raise ValidationError('planning increment must be positive')
        self.increment = increment
        self._plans: dict[str, RecoveryPlan] = {}
        self._occupations: dict[str, list[ResourceOccupation]] = {}

    def has_plan(self, closure_id: str) -> bool:
        return closure_id in self._plans

    def occupations_for(self, closure_id: str) -> tuple[ResourceOccupation, ...]:
        return tuple(self._occupations.get(closure_id, []))

    def plan(self, closure: Closure, analysis: ImpactAnalysis,
             snapshot: OperationsSnapshot, *, capacities: dict[str, int] | None = None,
             baseline: list[ResourceOccupation] | None = None) -> RecoveryPlan:
        if analysis.closure_id != closure.closure_id:
            raise ValidationError('impact analysis does not match closure')
        existing = self._plans.get(closure.closure_id)
        if existing is not None:
            return existing
        lift_at = closure.interval.end
        builder = _PlanBuilder(closure, analysis, snapshot, lift_at, self.increment)
        actions, occupations, missed = builder.build()
        phases = self._group_phases(actions)
        conflicts = self._detect_conflicts(occupations,
                                           capacities or {}, baseline or [], analysis)
        plan = RecoveryPlan(
            closure.closure_id, lift_at, phases, tuple(conflicts),
            tuple(sorted(occupations, key=lambda o: (o.resource, o.interval.start, o.owner))),
            tuple(missed), self._fingerprint(actions, conflicts),
        )
        self._plans[closure.closure_id] = plan
        self._occupations[closure.closure_id] = list(occupations)
        return plan

    @staticmethod
    def _fingerprint(actions, conflicts) -> tuple:
        return (
            tuple(sorted((a.action_id, a.item_id, a.kind.value, a.phase.value,
                          a.interval.start.isoformat(), a.interval.end.isoformat())
                         for a in actions)),
            tuple(sorted((c.resource, c.interval.start.isoformat(), c.interval.end.isoformat(),
                          c.owners, c.excess) for c in conflicts)),
        )

    @staticmethod
    def _group_phases(actions: list[RecoveryAction]) -> dict[RecoveryPhase, tuple[RecoveryAction, ...]]:
        out: dict[RecoveryPhase, list[RecoveryAction]] = {p: [] for p in RecoveryPhase}
        for action in actions:
            out[action.phase].append(action)
        return {p: tuple(sorted(items, key=lambda a: (a.interval.start, a.item_id)))
                for p, items in out.items()}

    def _detect_conflicts(self, occupations: list[ResourceOccupation],
                          capacities: dict[str, int], baseline: list[ResourceOccupation],
                          analysis: ImpactAnalysis) -> list[RecoveryConflict]:
        impacted = {i.item_id for i in analysis.impacts}
        # Untouched baseline uses stand; impacted items are removed from the
        # baseline because their recovery uses replace them.
        uses = [u for u in baseline if u.owner not in impacted]
        uses.extend(o for o in occupations if o.owner.startswith('recovery:'))
        resources = set(capacities) | {u.resource for u in uses}
        conflicts: list[RecoveryConflict] = []
        for resource in sorted(resources):
            capacity = capacities.get(resource, 1)
            relevant = [u for u in uses if u.resource == resource]
            points = sorted({p for u in relevant for p in (u.interval.start, u.interval.end)})
            for a, b in zip(points, points[1:]):
                active = [u for u in relevant if u.interval.start < b and a < u.interval.end]
                total = sum(u.quantity for u in active)
                if total > capacity:
                    conflicts.append(RecoveryConflict(
                        resource, TimeInterval(a, b),
                        tuple(sorted({u.owner for u in active})), total - capacity))
        return sorted(conflicts, key=lambda c: (c.interval.start, c.resource))


class _PlanBuilder:

    def __init__(self, closure: Closure, analysis: ImpactAnalysis,
                 snapshot: OperationsSnapshot, lift_at: datetime,
                 increment: timedelta):
        self.closure = closure
        self.analysis = analysis
        self.snapshot = snapshot
        self.lift_at = lift_at
        self.increment = increment
        self.actions: list[RecoveryAction] = []
        self.occupations: list[ResourceOccupation] = []
        self.missed: list[str] = []

    def build(self):
        self._schedule_calls()
        self._schedule_cranes()
        self._schedule_connections()
        self._schedule_handoffs()
        return self.actions, self.occupations, sorted(set(self.missed))

    def _round_up(self, at: datetime) -> datetime:
        seconds = self.increment.total_seconds()
        epoch = (at - self.lift_at).total_seconds()
        if epoch <= 0:
            return self.lift_at
        n = -(-epoch // seconds)
        return self.lift_at + timedelta(seconds=n * seconds)

    def _add(self, action: RecoveryAction) -> None:
        self.actions.append(action)
        if action.resource is None:
            return
        start, end = action.interval.start, action.interval.end
        if end <= start:
            end = start + timedelta(seconds=1)
        self.occupations.append(ResourceOccupation(
            f'recovery:{action.item_id}', action.resource,
            TimeInterval(start, end)))

    def _schedule_calls(self) -> None:
        # Two passes: in-flight calls resume at lift first, then delayed calls
        # queue per berth behind whatever already occupies it.
        vessel_impacts = self.analysis.of_kind(ImpactKind.VESSEL_SCHEDULE)
        berth_until: dict[str, datetime] = {}
        for impact in vessel_impacts:
            call = self.snapshot.call(impact.item_id)
            assert call is not None
            if call.state != ProgressState.STARTED:
                continue
            assert call.progressed_at is not None
            resume_end = max(call.window.end, self.lift_at + (call.window.end - call.progressed_at))
            interval = TimeInterval(self.lift_at, resume_end)
            self._add(RecoveryAction(
                f'resume-call-{call.call_id}', RecoveryPhase.RESUME, call.call_id,
                ImpactKind.VESSEL_SCHEDULE, interval,
                f'resume operations on alongside vessel {call.vessel}',
                f'berth:{call.berth_id}' if call.berth_id else None, locked=True))
            if call.berth_id:
                berth_until[call.berth_id] = max(berth_until.get(call.berth_id, self.lift_at), resume_end)
        cursor = self.lift_at
        for impact in sorted(vessel_impacts, key=lambda i: i.blocked_interval.start):
            call = self.snapshot.call(impact.item_id)
            assert call is not None
            if call.state == ProgressState.COMPLETED:
                continue
            if call.state == ProgressState.STARTED:
                continue
            earliest = cursor
            if call.berth_id:
                earliest = max(earliest, berth_until.get(call.berth_id, self.lift_at))
            start = self._round_up(max(earliest, call.window.start))
            end = start + call.window.duration
            interval = TimeInterval(start, end)
            self._add(RecoveryAction(
                f'berth-call-{call.call_id}', RecoveryPhase.CATCH_UP, call.call_id,
                ImpactKind.VESSEL_SCHEDULE, interval,
                f'berth delayed vessel {call.vessel} after closure',
                f'berth:{call.berth_id}' if call.berth_id else None))
            if call.berth_id:
                berth_until[call.berth_id] = end
            cursor = end

    def _schedule_cranes(self) -> None:
        # Two passes per crane: in-flight jobs resume at lift and claim the
        # crane cursor, replanned jobs queue behind them.
        crane_impacts = self.analysis.of_kind(ImpactKind.QUAY_CRANE)
        crane_until: dict[str, datetime] = {}
        for impact in crane_impacts:
            job = self.snapshot.crane_jobs[impact.item_id]
            if job.state != ProgressState.STARTED:
                continue
            assert job.progressed_at is not None
            resource = f'crane:{job.crane_id}'
            resume_end = max(job.window.end,
                             self.lift_at + (job.window.end - job.progressed_at))
            interval = TimeInterval(self.lift_at, resume_end)
            self._add(RecoveryAction(
                f'resume-job-{job.job_id}', RecoveryPhase.RESUME, job.job_id,
                ImpactKind.QUAY_CRANE, interval,
                f'restart crane {job.crane_id} on bay {job.bay}', resource, locked=True))
            crane_until[job.crane_id] = max(crane_until.get(job.crane_id, self.lift_at), resume_end)
        for impact in sorted(crane_impacts, key=lambda i: i.blocked_interval.start):
            job = self.snapshot.crane_jobs[impact.item_id]
            if job.state.locked:
                continue
            resource = f'crane:{job.crane_id}'
            start = self._round_up(crane_until.get(job.crane_id, self.lift_at))
            end = start + job.window.duration
            interval = TimeInterval(start, end)
            self._add(RecoveryAction(
                f'replan-job-{job.job_id}', RecoveryPhase.CATCH_UP, job.job_id,
                ImpactKind.QUAY_CRANE, interval,
                f'reschedule bay {job.bay} work on crane {job.crane_id}', resource))
            crane_until[job.crane_id] = end

    def _schedule_connections(self) -> None:
        call_end = {cid: self._recovered_call_end(cid) for cid in self.snapshot.calls}
        for impact in self.analysis.of_kind(ImpactKind.TRANSSHIPMENT):
            conn = next(c for c in self.snapshot.connections if c.container_id == impact.item_id)
            inbound_end = call_end[conn.inbound_call]
            outbound = self.snapshot.call(conn.outbound_call)
            assert outbound is not None
            ready = inbound_end + conn.transfer
            if ready > outbound.window.end or impact.disposition == Disposition.MISSED:
                self.missed.append(conn.container_id)
                self.actions.append(RecoveryAction(
                    f'miss-connection-{conn.container_id}', RecoveryPhase.NORMALIZE,
                    conn.container_id, ImpactKind.TRANSSHIPMENT,
                    TimeInterval(self.lift_at, self.lift_at + timedelta(minutes=1)),
                    f'rebook container {conn.container_id} to later sailing'))
                continue
            interval = TimeInterval(self._round_up(ready),
                                    self._round_up(ready) + timedelta(minutes=15))
            self._add(RecoveryAction(
                f'connect-{conn.container_id}', RecoveryPhase.NORMALIZE,
                conn.container_id, ImpactKind.TRANSSHIPMENT, interval,
                f'expedite transshipment of {conn.container_id}'))

    def _recovered_call_end(self, call_id: str) -> datetime:
        for action in self.actions:
            if action.kind == ImpactKind.VESSEL_SCHEDULE and action.item_id == call_id:
                return action.interval.end
        call = self.snapshot.call(call_id)
        assert call is not None
        # Unimpacted calls keep their original schedule.
        return call.window.end

    def _schedule_handoffs(self) -> None:
        for impact in self.analysis.of_kind(ImpactKind.LANDSIDE):
            handoff = self.snapshot.handoffs[impact.item_id]
            resource = f'{handoff.kind}_handoff:{handoff.reference}'
            if handoff.state == ProgressState.COMPLETED:
                continue
            if handoff.state == ProgressState.STARTED:
                assert handoff.progressed_at is not None
                nominal_end = handoff.progressed_at + timedelta(minutes=30)
                end = max(nominal_end, self.lift_at + timedelta(minutes=1))
                interval = TimeInterval(self.lift_at, end)
                self._add(RecoveryAction(
                    f'complete-handoff-{handoff.handoff_id}', RecoveryPhase.RESUME,
                    handoff.handoff_id, ImpactKind.LANDSIDE, interval,
                    f'complete in-progress {handoff.kind} handoff', resource, locked=True))
                continue
            start = self._round_up(max(
                self.lift_at, handoff.scheduled,
                self._recovered_call_end(handoff.reference)))
            interval = TimeInterval(start, start + timedelta(minutes=30))
            self._add(RecoveryAction(
                f'rebook-handoff-{handoff.handoff_id}', RecoveryPhase.NORMALIZE,
                handoff.handoff_id, ImpactKind.LANDSIDE, interval,
                f'reschedule {handoff.kind} handoff for {handoff.reference}', resource))
