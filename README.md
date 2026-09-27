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

`marine`, `cranes`, `yard`, `gate`, `customs`, `reefer`, `hazmat`, `equipment`, `rail`, `billing`, `transshipment`, `disruptions`, `planning`, `persistence`, `reporting`, and `api`.

## Disruption recovery

`terminalweave.disruptions` handles temporary channel or berth closures:

- `DisruptionManager.assess` finds every affected vessel schedule, quay-crane
  assignment, transshipment connection, and gate/rail handoff;
- started and completed items are locked and never moved backwards;
- `recover` produces a phased plan (`resume` → `catch_up` → `normalize`),
  queues catch-up work behind in-flight resource users, and reports any
  remaining resource overloads as explicit conflicts;
- replanning the same closure returns the cached plan, so repeated processing
  never creates a second set of resource occupations.
