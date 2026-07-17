# FABRIK-9 Full-Stack — Server-Authoritative Multi-Agent Factory

<!-- Replace OWNER/REPO with your GitHub slug once pushed. -->
[![CI](https://github.com/OWNER/REPO/actions/workflows/ci.yml/badge.svg)](https://github.com/OWNER/REPO/actions/workflows/ci.yml)

The browser version of FABRIK-9 converted to a production-shaped full-stack
application: the simulation now runs **on the server** (Python/FastAPI), the
browser is a pure renderer fed over **WebSocket at 30 fps**, and simulation
state + telemetry persist to a database (**SQLite for dev, PostgreSQL for
prod** — same code, one URL change).

```
Browser (Canvas renderer)  ⇄  WebSocket /ws (state @30fps, controls)
                              REST /api (runs, save/load, telemetry)
                                    │
                        FastAPI + SimulationManager
                        (authoritative 30 Hz tick loop)
                                    │
                        SQLAlchemy (async) → SQLite / PostgreSQL
```

## Why server-authoritative?

- **Many viewers, one truth** — every connected browser sees the same factory;
  clients cannot desync or cheat because they hold no simulation logic.
- **Persistence** — snapshots survive restarts; telemetry accumulates for
  offline analysis.
- **The pattern is the point** — authoritative loop + state broadcast +
  snapshot/restore is exactly how multiplayer game servers and robot-fleet
  dashboards are built.

## Quick start (zero config — SQLite)

```bash
cd backend
pip install -r requirements.txt
python -m uvicorn app.main:app --reload
# open http://127.0.0.1:8000  (frontend is served by the backend)
```

## Production (PostgreSQL)

```bash
export DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/fabrik9
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Tables are created automatically on startup; `backend/schema.sql` documents
the DDL (JSONB state, indexed `(run_id, tick)` telemetry).

## API

WebSocket `/ws`
- server → client: `world` (full grid; sent on connect and whenever
  `structure_version` changes), `state` (30 fps: agents + paths + decision
  traces, belt items, assembler progress, faults, stats, recent log)
- client → server: `{action: pause | resume | speed | inject_fault | chaos, value?}`

REST
- `POST /api/runs` `{seed?}` — new simulation (creates a run row)
- `GET  /api/state` — one-shot state
- `POST /api/runs/current/snapshots` `{label?}` — **Save**
- `GET  /api/snapshots` — list saves
- `POST /api/snapshots/{id}/load` — **Load** (sim resumes paused)
- `GET  /api/runs/{id}/telemetry` — historical samples
- `GET  /api/health`

## Design decisions worth knowing

**Snapshot consistency.** `Simulation.to_dict()` rolls back in-flight
construction — the blueprint's gears are refunded and the patch un-reserved,
like an uncommitted transaction — because half-built intent cannot be safely
resumed. A test (`test_snapshot_conserves_economy`) proves the rollback never
creates or destroys gears.

**Bandwidth discipline.** The static grid is only re-sent when
`structure_version` changes; the per-frame message carries dynamic entities
only. Steady state is a few KB/frame of JSON. (Binary encoding is the obvious
next optimization.)

**Fixed-timestep with accumulator.** Wall-clock jitter is absorbed by an
accumulator; a stall is clamped so the loop never spirals. Speed (1–8×)
multiplies steps per frame; broadcast stays at 30 fps.

**Determinism preserved.** Both LCG streams use exact integer arithmetic, so
Python runs are bit-reproducible per seed (asserted by tests). The Python and
JS ports are *behaviorally* equivalent — same regression criteria pass in
both — but not bit-identical to each other (A* tie-breaking and shuffle order
differ by language). Honest claim: each implementation is deterministic;
cross-language parity is at the outcome level.

## Testing

```bash
cd backend
pip install -r requirements.txt -r requirements-dev.txt

python -m pytest tests/ -q --cov=app --cov-report=term-missing
                                  # 39 tests: sim core (A*, auction, bootstrap,
                                  # fault recovery, determinism, snapshot
                                  # roundtrip + economy conservation), edge cases
                                  # (empty queue, collisions, duplicate bids,
                                  # rollback, WS disconnect), and the full server
                                  # stack (runtime loop, REST API, persistence).
python ../scripts/regress.py      # 16-seed regression (~4s)
python ../scripts/smoke_ws.py     # live integration: run the server first;
                                  # verifies WS rate, controls, save/load
```

Coverage is **~96%** of `app/`. Every server module (`api`, `db`, `main`,
`models`, `persistence`, `runtime`, `ws`) is at 100%; the simulation core is at
94%. The uncovered core lines are deliberate: rare defensive branches (an
unroutable belt plan, a fully-boxed-in agent that cannot side-step, the
chaos-mode auto-fault timer, world-gen bounds guards) whose failure modes are
already asserted through their happy paths.

### Continuous integration

`.github/workflows/ci.yml` runs the full suite **and** the 16-seed regression on
every push and pull request, across Python 3.11 and 3.12. The build fails on any
test failure, a failing regression seed, or a coverage drop below 90%.

## Repo layout

```
backend/app/sim/        DOM-free simulation core (Python port)
backend/app/runtime.py  authoritative tick loop + WebSocket hub
backend/app/api.py      REST endpoints
backend/app/ws.py       WebSocket endpoint + controls
backend/app/models.py   ORM (runs, snapshots, telemetry, events)
backend/schema.sql      PostgreSQL DDL reference
frontend/               static client: WS listener + Canvas renderer
scripts/                regression + live smoke test
```

## Roadmap

- Binary state encoding (MessagePack) and delta compression
- Multiple concurrent simulation rooms (`/ws/{run_id}`)
- Telemetry dashboard (charts from `/api/runs/{id}/telemetry`)
- Auth + rate limiting for the control channel
