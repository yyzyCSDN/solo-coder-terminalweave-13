"""Terminal disruption impact handling and phased recovery.

Public entry points:

* :class:`~terminalweave.disruptions.model.Disruption` — a channel/berth closure;
* :class:`~terminalweave.disruptions.impact.ImpactAnalyzer` — impact identification;
* :class:`~terminalweave.disruptions.recovery.RecoveryPlanner` — phased recovery plan;
* :class:`~terminalweave.disruptions.registry.DisruptionRegistry` — idempotent
  processing, action lifecycle and reservation bookkeeping.
"""
from .model import (
    Disruption,
    DisruptionKind,
    OperationsSnapshot,
    ScheduleEntry,
    CraneOperation,
    TransshipmentLink,
    LandsideHandover,
    HandoverKind,
    DisruptionImpact,
    ImpactSeverity,
    RecoveryPlan,
    RecoveryAction,
    RecoveryConflict,
    RecoveryPhase,
    ActionState,
)
from .impact import ImpactAnalyzer
from .recovery import RecoveryPlanner
from .registry import DisruptionRegistry, RecoveryActionTracker

__all__ = [
    'Disruption',
    'DisruptionKind',
    'OperationsSnapshot',
    'ScheduleEntry',
    'CraneOperation',
    'TransshipmentLink',
    'LandsideHandover',
    'HandoverKind',
    'DisruptionImpact',
    'ImpactSeverity',
    'RecoveryPlan',
    'RecoveryAction',
    'RecoveryConflict',
    'RecoveryPhase',
    'ActionState',
    'ImpactAnalyzer',
    'RecoveryPlanner',
    'DisruptionRegistry',
    'RecoveryActionTracker',
]
