from __future__ import annotations
from datetime import datetime

from terminalweave.core.clock import ensure_utc
from terminalweave.core.errors import ValidationError, StateTransitionError, NotFoundError

from .model import (
    Disruption,
    DisruptionImpact,
    OperationsSnapshot,
    RecoveryPlan,
    RecoveryAction,
    ActionState,
)
from .impact import ImpactAnalyzer
from .recovery import RecoveryPlanner


class RecoveryActionTracker:
    """Strict lifecycle for recovery actions.

    ``proposed -> started -> completed`` with monotonic actual timestamps.
    A proposed action may be cancelled; started work can never be cancelled,
    moved, or otherwise rolled back.
    """

    forward = {
        ActionState.PROPOSED: {ActionState.STARTED, ActionState.CANCELLED},
        ActionState.STARTED: {ActionState.COMPLETED},
        ActionState.COMPLETED: set(),
        ActionState.CANCELLED: set(),
    }

    def transition(
        self, action: RecoveryAction, target: ActionState, at: datetime
    ) -> RecoveryAction:
        at = ensure_utc(at)
        allowed = self.forward[action.state]
        if target not in allowed:
            raise StateTransitionError(
                f'recovery action cannot move {action.state.value} -> {target.value}'
            )
        if target is ActionState.STARTED:
            if action.state is not ActionState.PROPOSED:
                raise StateTransitionError('action was already started')
            action.state = ActionState.STARTED
            action.started_at = at
        elif target is ActionState.COMPLETED:
            if action.started_at is not None and at < action.started_at:
                raise ValidationError('completion cannot precede start')
            action.state = ActionState.COMPLETED
            action.completed_at = at
        elif target is ActionState.CANCELLED:
            action.state = ActionState.CANCELLED
        return action


class DisruptionRegistry:
    """Registers disruptions once and holds their current impact/plan.

    Re-processing an already registered disruption never creates a second
    plan or a second set of reservations: it revises the existing plan in
    place, carrying started/completed actions forward unchanged.
    """

    def __init__(
        self,
        analyzer: ImpactAnalyzer | None = None,
        planner: RecoveryPlanner | None = None,
    ):
        self.analyzer = analyzer or ImpactAnalyzer()
        self.planner = planner or RecoveryPlanner()
        self.tracker = RecoveryActionTracker()
        self._disruptions: dict[str, Disruption] = {}
        self._impacts: dict[str, DisruptionImpact] = {}
        self._plans: dict[str, RecoveryPlan] = {}
        self._snapshots: dict[str, OperationsSnapshot] = {}

    @property
    def disruption_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._disruptions))

    def register(self, disruption: Disruption) -> None:
        if disruption.disruption_id in self._disruptions:
            raise ValidationError('disruption already registered')
        self._disruptions[disruption.disruption_id] = disruption

    def get(self, disruption_id: str) -> Disruption:
        try:
            return self._disruptions[disruption_id]
        except KeyError as exc:
            raise NotFoundError('unknown disruption') from exc

    def process(
        self,
        disruption: Disruption,
        snapshot: OperationsSnapshot,
    ) -> RecoveryPlan:
        """Idempotent entry point: analyze impact and (re)build the plan."""
        existing = self._disruptions.get(disruption.disruption_id)
        if existing is not None and existing != disruption:
            # Same id, different closure — that is a new disruption, not a
            # re-run. The caller must revise the registered object explicitly.
            raise ValidationError('disruption id already registered with different terms')
        self._disruptions.setdefault(disruption.disruption_id, disruption)
        impact = self.analyzer.analyze(disruption, snapshot)
        self._impacts[disruption.disruption_id] = impact
        self._snapshots[disruption.disruption_id] = snapshot
        previous = self._plans.get(disruption.disruption_id)
        plan = self.planner.plan(disruption, impact, snapshot, previous)
        self._plans[disruption.disruption_id] = plan
        return plan

    def impact(self, disruption_id: str) -> DisruptionImpact:
        try:
            return self._impacts[disruption_id]
        except KeyError as exc:
            raise NotFoundError('disruption has not been analyzed') from exc

    def plan(self, disruption_id: str) -> RecoveryPlan:
        try:
            return self._plans[disruption_id]
        except KeyError as exc:
            raise NotFoundError('no recovery plan for disruption') from exc

    def update_action(
        self, disruption_id: str, action_id: str, target: ActionState, at: datetime
    ) -> RecoveryPlan:
        plan = self.plan(disruption_id)
        self.tracker.transition(plan.action(action_id), target, at)
        return plan

    def reprocess(self, disruption_id: str, snapshot: OperationsSnapshot) -> RecoveryPlan:
        """Recompute against a fresh snapshot without losing committed work."""
        disruption = self.get(disruption_id)
        return self.process(disruption, snapshot)

    def total_reservations(self) -> dict[str, int]:
        """Peak concurrent occupancy per resource across *all* live plans.

        Because reservations are derived from plan actions and action ids are
        unique per disruption, processing the same disruption twice cannot
        double-count its occupancy.
        """
        peaks: dict[str, int] = {}
        for plan in self._plans.values():
            intervals: dict[str, list] = {}
            for use in plan.reservations():
                intervals.setdefault(use.resource, []).append(use.interval)
            for resource, windows in intervals.items():
                points = sorted({p for w in windows for p in (w.start, w.end)})
                peak = 0
                for a, b in zip(points, points[1:]):
                    active = sum(1 for w in windows if w.start < b and a < w.end)
                    peak = max(peak, active)
                peaks[resource] = max(peaks.get(resource, 0), peak)
        return peaks
