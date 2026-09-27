from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from terminalweave.core.clock import ensure_utc
from terminalweave.core.errors import ValidationError
from terminalweave.core.intervals import TimeInterval


class ProgressState(str, Enum):
    """Execution state of a scheduled operational item.

    Only STARTED and COMPLETED carry a progress timestamp; those items are
    locked and recovery planning must never move them backwards.
    """
    PLANNED = 'planned'
    STARTED = 'started'
    COMPLETED = 'completed'

    @property
    def locked(self) -> bool:
        return self in {ProgressState.STARTED, ProgressState.COMPLETED}


@dataclass(frozen=True)
class ScheduledCall:
    call_id: str
    vessel: str
    berth_id: str | None
    channel_leg_ids: frozenset[str]
    window: TimeInterval
    state: ProgressState = ProgressState.PLANNED
    progressed_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, 'progressed_at',
                           ensure_utc(self.progressed_at) if self.progressed_at else None)
        if self.state.locked and self.progressed_at is None:
            raise ValidationError('started or completed call needs a progress timestamp')
        if self.progressed_at is not None and not self.window.contains(self.progressed_at) \
                and self.progressed_at != self.window.end:
            raise ValidationError('progress timestamp must fall inside the call window')


@dataclass(frozen=True)
class CraneAssignmentView:
    job_id: str
    call_id: str
    crane_id: str
    bay: int
    window: TimeInterval
    state: ProgressState = ProgressState.PLANNED
    progressed_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, 'progressed_at',
                           ensure_utc(self.progressed_at) if self.progressed_at else None)
        if self.state.locked and self.progressed_at is None:
            raise ValidationError('started or completed crane job needs a progress timestamp')


@dataclass(frozen=True)
class ConnectionView:
    """A planned transshipment rule: container leaves inbound_call on an
    outbound_call, needing transfer time after the inbound call is worked."""
    container_id: str
    inbound_call: str
    outbound_call: str
    transfer: object  # timedelta

    def __post_init__(self) -> None:
        if self.transfer.total_seconds() < 0:
            raise ValidationError('connection transfer cannot be negative')


@dataclass(frozen=True)
class LandsideHandoff:
    """A gate or rail interchange tied to a call or a container."""
    handoff_id: str
    kind: str
    reference: str
    scheduled: datetime
    state: ProgressState = ProgressState.PLANNED
    progressed_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, 'scheduled', ensure_utc(self.scheduled))
        object.__setattr__(self, 'progressed_at',
                           ensure_utc(self.progressed_at) if self.progressed_at else None)
        if self.kind not in {'gate', 'rail'}:
            raise ValidationError('handoff kind must be gate or rail')
        if self.state.locked and self.progressed_at is None:
            raise ValidationError('started or completed handoff needs a progress timestamp')


class OperationsSnapshot:
    """Immutable view of the operating plan assessed against a closure."""

    def __init__(self, *, calls: list[ScheduledCall], crane_jobs: list[CraneAssignmentView],
                 connections: list[ConnectionView], handoffs: list[LandsideHandoff]):
        _require_unique(calls, 'call_id', 'call id')
        _require_unique(crane_jobs, 'job_id', 'crane job id')
        _require_unique(handoffs, 'handoff_id', 'handoff id')
        if len({c.container_id for c in connections}) != len(connections):
            raise ValidationError('duplicate container connection')
        call_ids = {c.call_id for c in calls}
        for job in crane_jobs:
            if job.call_id not in call_ids:
                raise ValidationError(f'crane job {job.job_id} references unknown call')
        for conn in connections:
            if conn.inbound_call not in call_ids or conn.outbound_call not in call_ids:
                raise ValidationError('connection references unknown call')
            if conn.inbound_call == conn.outbound_call:
                raise ValidationError('connection cannot loop to the same call')
        self.calls = {c.call_id: c for c in calls}
        self.crane_jobs = {j.job_id: j for j in crane_jobs}
        self.connections = tuple(connections)
        self.handoffs = {h.handoff_id: h for h in handoffs}

    def call(self, call_id: str) -> ScheduledCall | None:
        return self.calls.get(call_id)

    def jobs_for(self, call_id: str) -> tuple[CraneAssignmentView, ...]:
        return tuple(j for j in self.crane_jobs.values() if j.call_id == call_id)


def _require_unique(items, attr: str, label: str) -> None:
    if len({getattr(x, attr) for x in items}) != len(list(items)):
        raise ValidationError(f'duplicate {label}')
