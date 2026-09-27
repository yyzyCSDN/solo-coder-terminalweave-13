from __future__ import annotations
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from terminalweave.core.clock import ensure_utc
from terminalweave.core.errors import ValidationError, StateTransitionError
from terminalweave.core.intervals import TimeInterval, merge_intervals


class ClosureStatus(str, Enum):
    ANNOUNCED = 'announced'
    ACTIVE = 'active'
    LIFTED = 'lifted'
    CANCELLED = 'cancelled'


@dataclass(frozen=True)
class Closure:
    """A temporary shutdown of a channel leg or a berth."""
    closure_id: str
    scope: str
    target_id: str
    interval: TimeInterval
    reason: str
    status: ClosureStatus = ClosureStatus.ANNOUNCED

    def __post_init__(self) -> None:
        if not self.closure_id.strip():
            raise ValidationError('closure id required')
        if self.scope not in {'channel', 'berth'}:
            raise ValidationError('closure scope must be channel or berth')
        if not self.target_id.strip():
            raise ValidationError('closure target required')
        if not self.reason.strip():
            raise ValidationError('closure reason required')

    @property
    def is_closed(self) -> bool:
        return self.status in {ClosureStatus.ANNOUNCED, ClosureStatus.ACTIVE}

    def with_status(self, status: ClosureStatus, at: datetime, lifted_early: bool = False) -> 'Closure':
        at = ensure_utc(at)
        if self.status == status:
            if status in {ClosureStatus.LIFTED, ClosureStatus.CANCELLED}:
                raise StateTransitionError('closure is already resolved')
            return self
        if self.status in {ClosureStatus.LIFTED, ClosureStatus.CANCELLED}:
            raise StateTransitionError('closure is already resolved')
        if status == ClosureStatus.LIFTED:
            end = at if lifted_early or at < self.interval.end else self.interval.end
            return replace(self, status=status, interval=TimeInterval(self.interval.start, end))
        if status == ClosureStatus.CANCELLED:
            return replace(self, status=status)
        if status == ClosureStatus.ACTIVE:
            if not self.interval.start <= at < self.interval.end:
                raise StateTransitionError('closure can only be activated inside its window')
            return replace(self, status=status)
        raise StateTransitionError(f'cannot transition closure to {status}')


class ClosureRegistry:
    """Registry of closures; re-registering the same id is idempotent."""

    def __init__(self):
        self._closures: dict[str, Closure] = {}

    def register(self, closure: Closure) -> Closure:
        existing = self._closures.get(closure.closure_id)
        if existing is not None:
            if existing.scope != closure.scope or existing.target_id != closure.target_id:
                raise ValidationError('closure id already registered for a different target')
            if existing.is_closed and closure.is_closed and existing.interval != closure.interval:
                raise ValidationError('active closure window cannot be redefined')
            return existing
        self._closures[closure.closure_id] = closure
        return closure

    def get(self, closure_id: str) -> Closure:
        try:
            return self._closures[closure_id]
        except KeyError as exc:
            raise ValidationError('unknown closure') from exc

    def mark(self, closure_id: str, status: ClosureStatus, at: datetime, lifted_early: bool = False) -> Closure:
        updated = self.get(closure_id).with_status(status, at, lifted_early)
        self._closures[closure_id] = updated
        return updated

    def for_target(self, scope: str, target_id: str) -> tuple[Closure, ...]:
        return tuple(c for c in self._closures.values() if c.scope == scope and c.target_id == target_id)

    def blocked_intervals(self, scope: str, target_id: str) -> list[TimeInterval]:
        return merge_intervals(
            c.interval for c in self._closures.values()
            if c.is_closed and c.scope == scope and c.target_id == target_id
        )
