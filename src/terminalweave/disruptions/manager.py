from __future__ import annotations
from datetime import datetime, timedelta
from .closures import Closure, ClosureRegistry, ClosureStatus
from .impact import ImpactAnalysis, ImpactAnalyzer
from .model import OperationsSnapshot
from .recovery import RecoveryPlan, RecoveryPlanner, ResourceOccupation


class DisruptionManager:
    """Single entry point: register closures, assess impact, plan recovery."""

    def __init__(self, increment: timedelta = timedelta(minutes=5)):
        self.registry = ClosureRegistry()
        self.analyzer = ImpactAnalyzer()
        self.planner = RecoveryPlanner(increment=increment)
        self._analyses: dict[str, ImpactAnalysis] = {}

    def declare(self, closure: Closure) -> Closure:
        return self.registry.register(closure)

    def assess(self, closure: Closure, snapshot: OperationsSnapshot, *,
               as_of: datetime | None = None) -> ImpactAnalysis:
        closure = self.registry.register(closure)
        analysis = self.analyzer.analyze(closure, snapshot, as_of=as_of)
        self._analyses[closure.closure_id] = analysis
        return analysis

    def reassess(self, closure_id: str, snapshot: OperationsSnapshot, *,
                 as_of: datetime | None = None) -> ImpactAnalysis:
        """Re-run impact analysis against a fresh snapshot.

        Already-produced recovery plans keep their occupations; reassessment
        never creates a second occupation set.
        """
        closure = self.registry.get(closure_id)
        analysis = self.analyzer.analyze(closure, snapshot, as_of=as_of)
        self._analyses[closure_id] = analysis
        return analysis

    def recover(self, closure_id: str, snapshot: OperationsSnapshot,
                *, capacities: dict[str, int] | None = None,
                baseline: list[ResourceOccupation] | None = None) -> RecoveryPlan:
        closure = self.registry.get(closure_id)
        analysis = self._analyses.get(closure_id)
        if analysis is None:
            analysis = self.assess(closure, snapshot)
        return self.planner.plan(closure, analysis, snapshot,
                                 capacities=capacities, baseline=baseline)

    def update_status(self, closure_id: str, status: ClosureStatus, at: datetime,
                      *, lifted_early: bool = False) -> Closure:
        return self.registry.mark(closure_id, status, at, lifted_early)


