from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from terminalweave.core.intervals import TimeInterval
from .closures import Closure
from .model import (
    OperationsSnapshot, ScheduledCall, CraneAssignmentView,
    ProgressState,
)


class ImpactKind(str, Enum):
    VESSEL_SCHEDULE = 'vessel_schedule'
    QUAY_CRANE = 'quay_crane'
    TRANSSHIPMENT = 'transshipment'
    LANDSIDE = 'landside'


class Disposition(str, Enum):
    """What recovery may do with an impacted item."""
    LOCKED = 'locked'            # started/completed: keep as-is, work the remainder after reopening
    REPLANNABLE = 'replannable'  # purely planned: free to move/reschedule
    MISSED = 'missed'            # connection/handoff can no longer be met


@dataclass(frozen=True)
class Impact:
    kind: ImpactKind
    item_id: str
    detail: str
    disposition: Disposition
    blocked_interval: TimeInterval
    locked: bool = False

    @property
    def replannable(self) -> bool:
        return self.disposition == Disposition.REPLANNABLE


@dataclass(frozen=True)
class ImpactAnalysis:
    closure_id: str
    impacts: tuple[Impact, ...]

    def of_kind(self, kind: ImpactKind) -> tuple[Impact, ...]:
        return tuple(i for i in self.impacts if i.kind == kind)

    @property
    def locked(self) -> tuple[Impact, ...]:
        return tuple(i for i in self.impacts if i.locked)

    @property
    def replannable(self) -> tuple[Impact, ...]:
        return tuple(i for i in self.impacts if i.replannable)

    @property
    def missed(self) -> tuple[Impact, ...]:
        return tuple(i for i in self.impacts if i.disposition == Disposition.MISSED)

    def item_ids(self, kind: ImpactKind) -> frozenset[str]:
        return frozenset(i.item_id for i in self.of_kind(kind))


class ImpactAnalyzer:
    """Finds every plan item touched by one channel/berth closure."""

    def analyze(self, closure: Closure, snapshot: OperationsSnapshot, *,
                as_of: datetime | None = None) -> ImpactAnalysis:
        blocked = closure.interval
        impacts: list[Impact] = []
        impacted_calls: set[str] = set()

        for call in snapshot.calls.values():
            impact = self._call_impact(closure, call, blocked, as_of)
            if impact is not None:
                impacts.append(impact)
                impacted_calls.add(call.call_id)

        for call_id in impacted_calls:
            for job in snapshot.jobs_for(call_id):
                impact = self._crane_impact(job, blocked)
                if impact is not None:
                    impacts.append(impact)

        impacts.extend(self._connection_impacts(snapshot, impacted_calls, blocked))
        impacts.extend(self._handoff_impacts(snapshot, impacted_calls, blocked))
        return ImpactAnalysis(closure.closure_id, tuple(impacts))

    def _call_impact(self, closure: Closure, call: ScheduledCall,
                     blocked: TimeInterval, as_of: datetime | None) -> Impact | None:
        hit = False
        if closure.scope == 'channel':
            hit = bool(call.channel_leg_ids & {closure.target_id}) and call.window.overlaps(blocked)
        elif call.berth_id == closure.target_id and call.window.overlaps(blocked):
            hit = True
        if not hit:
            return None
        overlap = call.window.intersection(blocked)
        assert overlap is not None
        disposition = Disposition.LOCKED if call.state.locked else Disposition.REPLANNABLE
        detail = f'{call.state.value} call {call.call_id} blocked during {closure.scope} closure'
        return Impact(ImpactKind.VESSEL_SCHEDULE, call.call_id, detail,
                      disposition, overlap, locked=call.state.locked)

    def _crane_impact(self, job: CraneAssignmentView, blocked: TimeInterval) -> Impact | None:
        overlap = job.window.intersection(blocked)
        if overlap is None:
            return None
        disposition = Disposition.LOCKED if job.state.locked else Disposition.REPLANNABLE
        detail = f'{job.state.value} crane job {job.job_id} on crane {job.crane_id} suspended'
        return Impact(ImpactKind.QUAY_CRANE, job.job_id, detail,
                      disposition, overlap, locked=job.state.locked)

    def _connection_impacts(self, snapshot: OperationsSnapshot,
                            impacted_calls: set[str], blocked: TimeInterval) -> list[Impact]:
        out: list[Impact] = []
        for conn in snapshot.connections:
            if conn.inbound_call not in impacted_calls and conn.outbound_call not in impacted_calls:
                continue
            inbound = snapshot.call(conn.inbound_call)
            outbound = snapshot.call(conn.outbound_call)
            assert inbound is not None and outbound is not None
            span = TimeInterval(inbound.window.start, outbound.window.end)
            interval = span.intersection(blocked) or blocked
            ready = inbound.window.end + conn.transfer
            if ready > outbound.window.end:
                disposition = Disposition.MISSED
                detail = (f'connection {conn.container_id}: inbound {conn.inbound_call} ready '
                          f'{ready.isoformat()} after outbound {conn.outbound_call} departs')
            else:
                disposition = Disposition.LOCKED if (inbound.state.locked or outbound.state.locked) \
                    else Disposition.REPLANNABLE
                detail = f'connection {conn.container_id} window compressed by closure'
            out.append(Impact(ImpactKind.TRANSSHIPMENT, conn.container_id, detail,
                              disposition, interval, locked=disposition == Disposition.LOCKED))
        return out

    def _handoff_impacts(self, snapshot: OperationsSnapshot,
                         impacted_calls: set[str], blocked: TimeInterval) -> list[Impact]:
        out: list[Impact] = []
        for handoff in snapshot.handoffs.values():
            # Landside work is only disrupted when it serves an impacted call;
            # gate and rail themselves keep running through a marine closure.
            if handoff.reference not in impacted_calls:
                continue
            service_window = TimeInterval(handoff.scheduled,
                                          handoff.scheduled + timedelta(minutes=30))
            interval = service_window.intersection(blocked) or blocked
            disposition = Disposition.LOCKED if handoff.state.locked else Disposition.REPLANNABLE
            detail = (f'{handoff.state.value} {handoff.kind} handoff {handoff.handoff_id} '
                      f'for {handoff.reference} disrupted')
            out.append(Impact(ImpactKind.LANDSIDE, handoff.handoff_id, detail,
                              disposition, interval, locked=handoff.state.locked))
        return out
