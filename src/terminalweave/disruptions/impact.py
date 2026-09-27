from __future__ import annotations
from .model import (
    Disruption,
    DisruptionKind,
    ImpactSeverity,
    OperationsSnapshot,
    ScheduleEntry,
    CraneOperation,
    CallImpact,
    CraneImpact,
    TransshipmentImpact,
    HandoverImpact,
    DisruptionImpact,
)


class ImpactAnalyzer:
    """Finds every schedule item touched by a channel/berth closure.

    Impacts come in two tiers:

    * ``DIRECT`` — the item's own planned window overlaps the closed resource;
    * ``CASCADING`` — the item only fails because an upstream call was directly
      blocked (crane work on a delayed call, a transshipment connection that
      loses its feeder, a landside handover feeding an impacted call).
    """

    def analyze(self, disruption: Disruption, snapshot: OperationsSnapshot) -> DisruptionImpact:
        if disruption.kind is DisruptionKind.CHANNEL:
            calls = self._channel_call_impacts(disruption, snapshot.calls)
        else:
            calls = self._berth_call_impacts(disruption, snapshot.calls)
        direct = {c.call_id for c in calls if c.severity is ImpactSeverity.DIRECT}
        all_calls = {c.call_id for c in calls}

        crane = self._crane_impacts(disruption, snapshot.crane_operations, all_calls, direct)
        crane_calls = {c.call_id for c in crane}
        trans = self._transshipment_impacts(
            snapshot.transshipments, all_calls, direct, crane_calls
        )
        handovers = self._handover_impacts(disruption, snapshot.handovers, all_calls, trans)

        return DisruptionImpact(
            disruption_id=disruption.disruption_id,
            calls=tuple(sorted(calls, key=lambda x: x.call_id)),
            crane_operations=tuple(sorted(crane, key=lambda x: x.job_id)),
            transshipments=tuple(sorted(trans, key=lambda x: x.container_id)),
            handovers=tuple(sorted(handovers, key=lambda x: x.handover_id)),
        )

    # -- vessel schedule ---------------------------------------------------

    def _channel_call_impacts(
        self, disruption: Disruption, entries: tuple[ScheduleEntry, ...]
    ) -> list[CallImpact]:
        out: list[CallImpact] = []
        for entry in entries:
            if entry.inbound_channel_window and entry.inbound_channel_window.overlaps(disruption.closure):
                out.append(
                    CallImpact(entry.call_id, ImpactSeverity.DIRECT, 'inbound transit window crosses channel closure')
                )
            elif entry.outbound_channel_window and entry.outbound_channel_window.overlaps(disruption.closure):
                out.append(
                    CallImpact(entry.call_id, ImpactSeverity.DIRECT, 'outbound transit window crosses channel closure')
                )
        return out

    def _berth_call_impacts(
        self, disruption: Disruption, entries: tuple[ScheduleEntry, ...]
    ) -> list[CallImpact]:
        out: list[CallImpact] = []
        for entry in entries:
            if entry.berth_id != disruption.target_id:
                continue
            if entry.berth_interval.overlaps(disruption.closure):
                out.append(
                    CallImpact(entry.call_id, ImpactSeverity.DIRECT, 'berth occupancy crosses berth closure')
                )
        return out

    # -- quay crane work ---------------------------------------------------

    def _crane_impacts(
        self,
        disruption: Disruption,
        operations: tuple[CraneOperation, ...],
        impacted_calls: set[str],
        direct_calls: set[str],
    ) -> list[CraneImpact]:
        out: list[CraneImpact] = []
        for op in operations:
            direct = (
                disruption.kind is DisruptionKind.BERTH
                and op.call_id in direct_calls
                and op.interval.overlaps(disruption.closure)
            )
            if direct:
                out.append(
                    CraneImpact(
                        op.job_id, op.call_id, op.crane_id,
                        ImpactSeverity.DIRECT,
                        'crane work window crosses berth closure',
                    )
                )
            elif op.call_id in impacted_calls and op.interval.end > disruption.starts_at:
                out.append(
                    CraneImpact(
                        op.job_id, op.call_id, op.crane_id,
                        ImpactSeverity.CASCADING,
                        'crane work depends on a vessel call delayed by the closure',
                    )
                )
        return out

    # -- transshipment connections ----------------------------------------

    def _transshipment_impacts(
        self,
        links: tuple,
        impacted_calls: set[str],
        direct_calls: set[str],
        crane_calls: set[str],
    ) -> list[TransshipmentImpact]:
        out: list[TransshipmentImpact] = []
        for link in links:
            inbound_hit = link.inbound_call in impacted_calls
            outbound_hit = link.outbound_call in impacted_calls
            if not (inbound_hit or outbound_hit):
                continue
            if link.inbound_call in direct_calls or link.outbound_call in direct_calls:
                severity = ImpactSeverity.DIRECT
                detail = 'connection leg is on a directly blocked vessel call'
            elif link.inbound_call in crane_calls:
                severity = ImpactSeverity.CASCADING
                detail = 'feeder discharge is delayed by rescheduled crane work'
            else:
                severity = ImpactSeverity.CASCADING
                detail = 'connection depends on a vessel call delayed by the closure'
            out.append(
                TransshipmentImpact(
                    link.container_id, link.inbound_call, link.outbound_call, severity, detail
                )
            )
        return out

    # -- landside handovers ------------------------------------------------

    def _handover_impacts(
        self,
        disruption: Disruption,
        handovers: tuple,
        impacted_calls: set[str],
        trans_impacts: list[TransshipmentImpact],
    ) -> list[HandoverImpact]:
        affected_containers = {t.container_id for t in trans_impacts}
        out: list[HandoverImpact] = []
        for h in handovers:
            feeds_call = h.call_id in impacted_calls
            feeds_container = bool(h.container_ids & affected_containers)
            in_window = disruption.closure.contains(h.scheduled_at)
            if not (feeds_call or feeds_container or in_window):
                continue
            if in_window and h.call_id is None and not feeds_container:
                severity = ImpactSeverity.DIRECT
                detail = f'{h.kind.value} handover is scheduled inside the closure window'
            elif feeds_call:
                severity = ImpactSeverity.CASCADING
                detail = 'handover feeds a vessel call delayed by the closure'
            else:
                severity = ImpactSeverity.CASCADING
                detail = 'handover carries cargo from an impacted transshipment'
            out.append(HandoverImpact(h.handover_id, h.kind, severity, detail))
        return out
