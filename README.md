# TerminalWeave

TerminalWeave is a dependency-light Python engine for modelling day-to-day container-terminal operations. It covers marine access, berth use, quay cranes, yard placement, truck-gate appointments, customs holds, reefer monitoring, hazardous-goods segregation, equipment maintenance, rail blocks, demurrage billing and transshipment connections.

The package is intentionally usable without a database or web framework. Domain services expose ordinary Python APIs, while the persistence layer supplies an append-only event store and snapshots for embedding in larger systems.

## Quick start

```bash
python -m pip install -e ".[test]"
python -m terminalweave.cli demo
python -m pytest -q
```

Run the small HTTP adapter with `terminalweave serve`. It exposes `GET /health`,
`POST /tide/clearance`, and `POST /billing/demurrage`. The other packages are
library components intended to be composed by an embedding application.

## Design principles

- domain calculations are deterministic for identical inputs;
- time values are timezone-aware UTC datetimes;
- operational state transitions are explicit and validated;
- safety constraints fail closed when required information is absent;
- money uses `Decimal`;
- core domain modules depend only on the Python standard library.

## Main packages

`marine`, `cranes`, `yard`, `gate`, `customs`, `reefer`, `hazmat`, `equipment`, `rail`, `billing`, `transshipment`, `planning`, `persistence`, `reporting`, `api`, and `disruptions`.

## Disruption recovery

The `disruptions` package handles temporary channel or berth closures:

- `ImpactAnalyzer` identifies the affected vessel calls (direct), quay-crane
  work, transshipment connections and landside handovers (cascading);
- `RecoveryPlanner` produces a phased plan — containment, resumption,
  catch-up, normalization — with deterministic per-action resource occupancy;
- resource clashes against the baseline plan, other recovery actions, or the
  closure window are returned as explicit `RecoveryConflict`s instead of being
  silently rescheduled;
- `DisruptionRegistry` processes each disruption idempotently: re-running an
  analysis revises the existing plan (revision counter increments), never
  creates a second set of reservations, and carries started/completed actions
  forward verbatim via a monotonic `proposed → started → completed` lifecycle.
