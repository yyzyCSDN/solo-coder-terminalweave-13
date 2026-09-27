from __future__ import annotations
from dataclasses import replace
from datetime import timedelta

from terminalweave.core.errors import ValidationError
from terminalweave.core.intervals import TimeInterval
from terminalweave.planning.conflicts import ConflictDetector, ResourceUse

from .model import (
    Disruption,
    DisruptionKind,
    OperationsSnapshot,
    DisruptionImpact,
    RecoveryAction,
    RecoveryConflict,
    RecoveryPhase,
    RecoveryPlan,
    ActionState,
    PHASE_CONTAINMENT,
    PHASE_RESUMPTION,
    PHASE_CATCH_UP,
    PHASE_NORMALIZATION,
    PHASE_ORDER,
    ACT_HOLD,
    ACT_CHANNEL_TRANSIT,
    ACT_REBERTH,
    ACT_RESUME_CRANE,
    ACT_EXPEDITE,
    ACT_REBOOK,
    ACT_RESCHEDULE_HANDOVER,
    HandoverKind,
)


def _channel_resource(channel_id: str) -> str:
    return f'channel:{channel_id}'


def _berth_resource(berth_id: str) -> str:
    return f'berth:{berth_id}'


class RecoveryPlanner:
    """Builds and revises a phased recovery plan for one disruption.

    Guarantees:

    * action ids are deterministic, so re-running the planner for the same
      disruption merges with the prior plan instead of emitting a second set
      of resource reservations;
    * once an action is started/completed it is carried forward verbatim —
      committed work is never moved backwards or rescheduled;
    * every capacity clash between recovery actions, the baseline plan and the
      closure itself is returned as an explicit ``RecoveryConflict`` rather
      than being silently resolved.
    """

    def __init__(
        self,
        transit_hours: float = 1.0,
        berth_hours: float = 2.0,
        expedite_hours: float = 1.0,
        handover_hours: float = 0.5,
        stagger: timedelta = timedelta(minutes=30),
        resource_capacity: dict[str, int] | None = None,
    ):
        if min(transit_hours, berth_hours, expedite_hours, handover_hours) <= 0:
            raise ValidationError('recovery durations must be positive')
        if stagger.total_seconds() < 0:
            raise ValidationError('stagger cannot be negative')
        self.transit = timedelta(hours=transit_hours)
        self.berth_work = timedelta(hours=berth_hours)
        self.expedite_work = timedelta(hours=expedite_hours)
        self.handover_work = timedelta(hours=handover_hours)
        self.stagger = stagger
        # Fail closed: shared resources without an explicit capacity default
        # to a single slot, forcing clashes to surface as conflicts.
        self.resource_capacity = dict(resource_capacity or {})

    # -- public API --------------------------------------------------------

    def plan(
        self,
        disruption: Disruption,
        impact: DisruptionImpact,
        snapshot: OperationsSnapshot,
        previous: RecoveryPlan | None = None,
    ) -> RecoveryPlan:
        if impact.disruption_id != disruption.disruption_id:
            raise ValidationError('impact does not belong to the disruption')
        if previous is not None and previous.disruption_id != disruption.disruption_id:
            raise ValidationError('previous plan belongs to another disruption')

        actions_by_id = self._locked_actions(previous)
        actions_by_id.update(
            self._generate(disruption, impact, snapshot, set(actions_by_id))
        )
        # Deterministic ordering; deterministic ids — identical inputs always
        # yield the same action set and therefore the same resource occupancy.
        actions = [actions_by_id[k] for k in sorted(actions_by_id)]
        conflicts = self._detect_conflicts(disruption, actions, snapshot)
        phases = self._build_phases(disruption, actions)
        revision = (previous.revision + 1) if previous is not None else 1
        return RecoveryPlan(disruption.disruption_id, actions, conflicts, phases, revision)

    # -- committed work ----------------------------------------------------

    def _locked_actions(self, previous: RecoveryPlan | None) -> dict[str, RecoveryAction]:
        if previous is None:
            return {}
        return {
            a.action_id: replace(a)
            for a in previous.actions
            if a.locked
        }

    # -- action generation -------------------------------------------------

    def _generate(
        self,
        disruption: Disruption,
        impact: DisruptionImpact,
        snapshot: OperationsSnapshot,
        locked: dict[str, RecoveryAction],
    ) -> dict[str, RecoveryAction]:
        entries = {c.call_id: c for c in snapshot.calls}
        rank = {call_id: i for i, call_id in enumerate(sorted(impact.affected_call_ids))}
        delays = self._call_delays(disruption, impact, snapshot, rank)
        out: dict[str, RecoveryAction] = {}

        def add(action: RecoveryAction) -> None:
            if action.action_id in locked:
                # The regenerated version of committed work is discarded:
                # the locked copy is the single source of truth.
                return
            out[action.action_id] = action

        closure_end = disruption.ends_at
        for call_impact in impact.calls:
            entry = entries.get(call_impact.call_id)
            slot = rank[call_impact.call_id]
            if disruption.kind is DisruptionKind.CHANNEL:
                hold_start = min(entry.berth_interval.start, disruption.starts_at) if entry else disruption.starts_at
                if hold_start < closure_end:
                    add(self._action(
                        disruption, call_impact.call_id, ACT_HOLD,
                        PHASE_CONTAINMENT,
                        f'hold vessel {call_impact.call_id} at anchorage while channel is closed',
                        TimeInterval(hold_start, closure_end), None,
                    ))
                transit_start = closure_end + slot * self.stagger
                add(self._action(
                    disruption, call_impact.call_id, ACT_CHANNEL_TRANSIT,
                    PHASE_RESUMPTION,
                    f'transit vessel {call_impact.call_id} through {disruption.target_id} after reopening',
                    TimeInterval(transit_start, transit_start + self.transit),
                    _channel_resource(disruption.target_id),
                ))
            else:
                if entry is not None:
                    hold_start = min(max(entry.berth_interval.start, disruption.starts_at), closure_end)
                else:
                    hold_start = disruption.starts_at
                if hold_start < closure_end:
                    add(self._action(
                        disruption, call_impact.call_id, ACT_HOLD,
                        PHASE_CONTAINMENT,
                        f'suspend operations for {call_impact.call_id} while berth is closed',
                        TimeInterval(hold_start, closure_end), None,
                    ))
                berth_start = closure_end + slot * self.stagger
                add(self._action(
                    disruption, call_impact.call_id, ACT_REBERTH,
                    PHASE_RESUMPTION,
                    f're-berth vessel {call_impact.call_id} at {disruption.target_id}',
                    TimeInterval(berth_start, berth_start + self.berth_work),
                    _berth_resource(disruption.target_id),
                ))

        crane_ops = {op.job_id: op for op in snapshot.crane_operations}
        for crane_impact in impact.crane_operations:
            delay = delays.get(crane_impact.call_id, timedelta())
            original = crane_ops.get(crane_impact.job_id)
            duration = original.interval.duration if original else self.berth_work
            if crane_impact.severity.value == 'direct':
                start = closure_end
                note = 'resume interrupted quay-crane work after reopening'
            else:
                base = original.interval.start if original else closure_end
                start = max(base, disruption.starts_at) + delay
                note = 'reschedule crane work behind the delayed vessel call'
            add(self._action(
                disruption, crane_impact.job_id, ACT_RESUME_CRANE,
                PHASE_CATCH_UP, note,
                TimeInterval(start, start + duration),
                f'crane:{crane_impact.crane_id}',
                target_ref=crane_impact.job_id,
            ))

        links = {t.container_id: t for t in snapshot.transshipments}
        for trans in impact.transshipments:
            link = links[trans.container_id]
            delay = delays.get(link.inbound_call, timedelta())
            delayed_ready = max(link.ready_at + delay, closure_end)
            if delayed_ready + self.expedite_work <= link.outbound_departure:
                add(self._action(
                    disruption, trans.container_id, ACT_EXPEDITE,
                    PHASE_CATCH_UP,
                    f'expedite yard transfer for {trans.container_id} to keep the connection',
                    TimeInterval(delayed_ready, delayed_ready + self.expedite_work),
                    'yard:transfer',
                    target_ref=trans.container_id,
                ))
            else:
                # The box cannot physically make the outbound sailing;
                # rebooking is a decision and occupies no resource.
                add(self._action(
                    disruption, trans.container_id, ACT_REBOOK,
                    PHASE_NORMALIZATION,
                    f'rebook {trans.container_id} from {trans.outbound_call} to the next sailing',
                    None, None,
                    target_ref=trans.container_id,
                ))

        handovers = {h.handover_id: h for h in snapshot.handovers}
        for handover in impact.handovers:
            source = handovers[handover.handover_id]
            delay = delays.get(source.call_id, timedelta()) if source.call_id else timedelta()
            when = max(source.scheduled_at + delay, closure_end)
            resource = 'gate:lane' if handover.kind is HandoverKind.GATE else 'rail:interchange'
            add(self._action(
                disruption, handover.handover_id, ACT_RESCHEDULE_HANDOVER,
                PHASE_CATCH_UP,
                f'reschedule {handover.kind.value} handover {handover.handover_id} behind recovery work',
                TimeInterval(when, when + self.handover_work),
                resource,
                target_ref=handover.handover_id,
            ))

        return out

    def _action(
        self,
        disruption: Disruption,
        key: str,
        action_type: str,
        phase: str,
        summary: str,
        interval: TimeInterval | None,
        resource: str | None,
        target_ref: str | None = None,
    ) -> RecoveryAction:
        ref = target_ref or key
        action_id = f'{disruption.disruption_id}:{ref}:{action_type}'
        return RecoveryAction(
            action_id=action_id,
            phase=phase,
            action_type=action_type,
            target_ref=ref,
            summary=summary,
            interval=interval,
            resource=resource,
        )

    # -- delay propagation -------------------------------------------------

    def _call_delays(
        self,
        disruption: Disruption,
        impact: DisruptionImpact,
        snapshot: OperationsSnapshot,
        rank: dict[str, int],
    ) -> dict[str, timedelta]:
        """Minimum shift that clears the closure, per impacted call."""
        entries = {c.call_id: c for c in snapshot.calls}
        delays: dict[str, timedelta] = {}
        for call_id in sorted(impact.affected_call_ids):
            entry = entries.get(call_id)
            if entry is None:
                continue
            resume_at = disruption.ends_at + rank[call_id] * self.stagger
            if disruption.kind is DisruptionKind.CHANNEL:
                windows = [w for w in (entry.inbound_channel_window, entry.outbound_channel_window) if w]
                anchor = min((w.start for w in windows), default=disruption.starts_at)
            else:
                anchor = entry.berth_interval.start
            delays[call_id] = max(timedelta(), resume_at - anchor)
        return delays

    # -- conflicts ---------------------------------------------------------

    def _detect_conflicts(
        self,
        disruption: Disruption,
        actions: list[RecoveryAction],
        snapshot: OperationsSnapshot,
    ) -> list[RecoveryConflict]:
        uses: list[ResourceUse] = list(snapshot.baseline_uses)  # committed baseline
        for action in actions:
            if action.state is ActionState.CANCELLED or action.resource is None or action.interval is None:
                continue
            uses.append(ResourceUse(owner=action.action_id, resource=action.resource, interval=action.interval))

        # The closed resource cannot serve anyone during the closure: inject
        # the closure itself as an owner so any overlap is reported loudly.
        closure_resource = (
            _channel_resource(disruption.target_id)
            if disruption.kind is DisruptionKind.CHANNEL
            else _berth_resource(disruption.target_id)
        )
        closure_owner = f'{disruption.disruption_id}:closure'
        uses.append(ResourceUse(owner=closure_owner, resource=closure_resource, interval=disruption.closure))

        resources = {u.resource for u in uses}
        capacities = {r: self.resource_capacity.get(r, 1) for r in resources}

        found = ConflictDetector().detect(uses, capacities)
        out: list[RecoveryConflict] = []
        for c in found:
            if closure_owner in c.owners:
                detail = 'recovery work still occupies the resource while it is closed'
            else:
                detail = 'recovery work overlaps committed operations'
            out.append(RecoveryConflict(c.resource, c.interval, c.owners, c.excess, detail))
        return sorted(out, key=lambda x: (x.resource, x.interval.start, x.owners))

    # -- phases ------------------------------------------------------------

    def _build_phases(self, disruption: Disruption, actions: list[RecoveryAction]) -> list[RecoveryPhase]:
        phases: list[RecoveryPhase] = []
        grouped: dict[str, list[RecoveryAction]] = {name: [] for name in PHASE_ORDER}
        for action in actions:
            grouped[action.phase].append(action)
        for name, members in grouped.items():
            if not members:
                continue
            timed = [a for a in members if a.interval is not None]
            if timed:
                start = min(a.interval.start for a in timed)
                end = max(a.interval.end for a in timed)
            else:
                # Decision-only phase (e.g. pure rebooking): anchor it at the
                # closure end so the phase ordering stays meaningful.
                start = disruption.ends_at
                end = disruption.ends_at + timedelta(seconds=1)
            if name == PHASE_CONTAINMENT:
                start = min(start, disruption.starts_at)
                end = max(end, disruption.ends_at)
            window = TimeInterval(start, max(end, start + timedelta(seconds=1)))
            phases.append(RecoveryPhase(name, window, tuple(a.action_id for a in members)))
        order = {name: i for i, name in enumerate(PHASE_ORDER)}
        phases.sort(key=lambda p: order[p.name])
        return phases
