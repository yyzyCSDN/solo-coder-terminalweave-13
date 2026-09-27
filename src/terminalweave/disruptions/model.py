from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from terminalweave.core.clock import ensure_utc
from terminalweave.core.errors import ValidationError, StateTransitionError, NotFoundError
from terminalweave.core.intervals import TimeInterval


# ---------------------------------------------------------------------------
# Disruption declaration
# ---------------------------------------------------------------------------

class DisruptionKind(str, Enum):
    CHANNEL = 'channel'
    BERTH = 'berth'


@dataclass(frozen=True)
class Disruption:
    """A temporary closure of a channel segment or a berth."""
    disruption_id: str
    kind: DisruptionKind
    target_id: str
    closure: TimeInterval
    reason: str = ''

    def __post_init__(self) -> None:
        if not self.disruption_id.strip():
            raise ValidationError('disruption id required')
        if not self.target_id.strip():
            raise ValidationError('closure target required')
        if not isinstance(self.kind, DisruptionKind):
            raise ValidationError('invalid disruption kind')

    @property
    def starts_at(self) -> datetime:
        return self.closure.start

    @property
    def ends_at(self) -> datetime:
        return self.closure.end


# ---------------------------------------------------------------------------
# Operating snapshot (planned state fed in by the embedding application)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ScheduleEntry:
    """A vessel call plan: assigned berth plus its channel movement windows."""
    call_id: str
    berth_id: str
    berth_interval: TimeInterval
    inbound_channel_window: TimeInterval | None = None
    outbound_channel_window: TimeInterval | None = None


@dataclass(frozen=True)
class CraneOperation:
    job_id: str
    call_id: str
    crane_id: str
    interval: TimeInterval


@dataclass(frozen=True)
class TransshipmentLink:
    container_id: str
    inbound_call: str
    outbound_call: str
    ready_at: datetime
    outbound_departure: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, 'ready_at', ensure_utc(self.ready_at))
        object.__setattr__(self, 'outbound_departure', ensure_utc(self.outbound_departure))


class HandoverKind(str, Enum):
    GATE = 'gate'
    RAIL = 'rail'


@dataclass(frozen=True)
class LandsideHandover:
    """A landside interchange (truck appointment slot or rail cutoff)."""
    handover_id: str
    kind: HandoverKind
    scheduled_at: datetime
    call_id: str | None = None
    container_ids: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, 'scheduled_at', ensure_utc(self.scheduled_at))
        if not isinstance(self.kind, HandoverKind):
            raise ValidationError('invalid handover kind')


@dataclass(frozen=True)
class OperationsSnapshot:
    calls: tuple[ScheduleEntry, ...] = ()
    crane_operations: tuple[CraneOperation, ...] = ()
    transshipments: tuple[TransshipmentLink, ...] = ()
    handovers: tuple[LandsideHandover, ...] = ()
    """Resource uses already committed by the baseline plan (never moved)."""
    baseline_uses: tuple[Any, ...] = ()

    def __post_init__(self) -> None:
        if len({c.call_id for c in self.calls}) != len(self.calls):
            raise ValidationError('duplicate schedule call')
        if len({j.job_id for j in self.crane_operations}) != len(self.crane_operations):
            raise ValidationError('duplicate crane operation')
        if len({h.handover_id for h in self.handovers}) != len(self.handovers):
            raise ValidationError('duplicate landside handover')
        if len({t.container_id for t in self.transshipments}) != len(self.transshipments):
            raise ValidationError('duplicate transshipment container')


# ---------------------------------------------------------------------------
# Impact findings
# ---------------------------------------------------------------------------

class ImpactSeverity(str, Enum):
    DIRECT = 'direct'
    CASCADING = 'cascading'


@dataclass(frozen=True)
class CallImpact:
    call_id: str
    severity: ImpactSeverity
    blocked_reason: str


@dataclass(frozen=True)
class CraneImpact:
    job_id: str
    call_id: str
    crane_id: str
    severity: ImpactSeverity
    blocked_reason: str


@dataclass(frozen=True)
class TransshipmentImpact:
    container_id: str
    inbound_call: str
    outbound_call: str
    severity: ImpactSeverity
    detail: str


@dataclass(frozen=True)
class HandoverImpact:
    handover_id: str
    kind: HandoverKind
    severity: ImpactSeverity
    detail: str


@dataclass(frozen=True)
class DisruptionImpact:
    disruption_id: str
    calls: tuple[CallImpact, ...]
    crane_operations: tuple[CraneImpact, ...]
    transshipments: tuple[TransshipmentImpact, ...]
    handovers: tuple[HandoverImpact, ...]

    @property
    def affected_call_ids(self) -> frozenset[str]:
        return frozenset(c.call_id for c in self.calls)

    @property
    def affected_job_ids(self) -> frozenset[str]:
        return frozenset(j.job_id for j in self.crane_operations)

    @property
    def is_empty(self) -> bool:
        return not (self.calls or self.crane_operations or self.transshipments or self.handovers)


# ---------------------------------------------------------------------------
# Recovery plan
# ---------------------------------------------------------------------------

class ActionState(str, Enum):
    PROPOSED = 'proposed'
    STARTED = 'started'
    COMPLETED = 'completed'
    CANCELLED = 'cancelled'


PHASE_CONTAINMENT = 'containment'
PHASE_RESUMPTION = 'resumption'
PHASE_CATCH_UP = 'catch_up'
PHASE_NORMALIZATION = 'normalization'
PHASE_ORDER = (PHASE_CONTAINMENT, PHASE_RESUMPTION, PHASE_CATCH_UP, PHASE_NORMALIZATION)

# Deterministic action type tags.
ACT_HOLD = 'hold'
ACT_CHANNEL_TRANSIT = 'channel_transit'
ACT_REBERTH = 'reberth'
ACT_RESUME_CRANE = 'resume_crane'
ACT_EXPEDITE = 'expedite_connection'
ACT_REBOOK = 'rebook_connection'
ACT_RESCHEDULE_HANDOVER = 'reschedule_handover'


@dataclass
class RecoveryAction:
    action_id: str
    phase: str
    action_type: str
    target_ref: str
    summary: str
    interval: TimeInterval | None = None
    resource: str | None = None
    state: ActionState = ActionState.PROPOSED
    started_at: datetime | None = None
    completed_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.phase not in PHASE_ORDER:
            raise ValidationError(f'unknown recovery phase: {self.phase}')

    @property
    def locked(self) -> bool:
        """Started/completed actions are committed work and must never regress."""
        return self.state in {ActionState.STARTED, ActionState.COMPLETED}


@dataclass(frozen=True)
class RecoveryConflict:
    resource: str
    interval: TimeInterval
    owners: tuple[str, ...]
    excess: int
    detail: str


@dataclass(frozen=True)
class RecoveryPhase:
    name: str
    window: TimeInterval
    action_ids: tuple[str, ...]


@dataclass
class RecoveryPlan:
    disruption_id: str
    actions: list[RecoveryAction]
    conflicts: list[RecoveryConflict]
    phases: list[RecoveryPhase]
    revision: int = 1

    def action(self, action_id: str) -> RecoveryAction:
        for a in self.actions:
            if a.action_id == action_id:
                return a
        raise NotFoundError(f'unknown recovery action {action_id}')

    def by_phase(self, name: str) -> tuple[RecoveryAction, ...]:
        ids: set[str] = set()
        for p in self.phases:
            if p.name == name:
                ids.update(p.action_ids)
        return tuple(a for a in self.actions if a.action_id in ids)

    def reservations(self) -> tuple[Any, ...]:
        """Resource occupancy generated by this plan — derived, never duplicated.

        Cancelled actions occupy nothing. Waiting/holds occupy nothing.
        """
        from terminalweave.planning.conflicts import ResourceUse
        out: list[Any] = []
        for a in self.actions:
            if a.state == ActionState.CANCELLED or a.resource is None or a.interval is None:
                continue
            out.append(ResourceUse(owner=a.action_id, resource=a.resource, interval=a.interval))
        return tuple(out)
