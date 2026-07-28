# Architecture

## Current dependency direction

The refactor uses a simple layered structure without adding a container framework:

```text
process entrypoint
  main.py
    ↓
application orchestration
  kiwoom_stock.application.runtime
  kiwoom_stock.application.lifecycle
  kiwoom_stock.application.ports
  kiwoom_stock.application.session
    ↓
domain/business rules
  kiwoom_stock.domain.*
  kiwoom_stock.monitoring.strategy
  kiwoom_stock.core.physics_engine
  kiwoom_stock.core.state_manager
    ↓
infrastructure adapters
  kiwoom_stock.api.*
  kiwoom_stock.infrastructure.*
  kiwoom_stock.monitoring.notifier
  kiwoom_stock.monitoring.reporter
  kiwoom_stock.utils.*
```

`main.py` is the process composition root. It validates a frozen settings snapshot without mutation, reads one startup
date, passes that date to the KRX calendar, and activates the runtime graph only on an open session. It is also the outer
composition root for `DailyReporter`, `S3Manager`, legacy retention, and identity-scoped cleanup;
`application.lifecycle` imports none of those concrete adapters. The engine returns a frozen `TradingSessionResult`
from `application.session`; the composition root permits post-market work only for normal market-close or
user-interrupt outcomes and exits `1` immediately for a kill-switch outcome. Every path that constructed an engine
attempts engine shutdown before post-market routing or process exit.

## Composition roots

- `kiwoom_stock.application.runtime.create_trading_runtime(...)`
  - activates an already validated settings object, or validates through the compatibility path for legacy callers;
  - creates dated output before publishing legacy config views;
  - creates one `TradeLogger(settings.database.path)` before any Kiwoom client, so an unusable configured SQLite path
    fails before external authentication;
  - wraps that same logger with `AsyncPhysicalStateRepository` and creates `KiwoomClient`;
  - injects the exact ledger and physical-state repository objects into `TradingEngine`;
  - closes already-created persistence resources in reverse order if later construction fails, while preserving the
    construction error as primary.
- `kiwoom_stock.application.lifecycle.run_post_market_tasks(...)`
  - is entered only after `TradingEngine.close()` succeeds;
  - runs the daily post-mortem;
  - obtains an immutable per-target archive receipt in production-class environments (`prod`, `production-like`);
  - permits scoped cleanup only after a non-empty all-success receipt;
  - preserves the legacy three-day retention cleanup outside production-class environments;
  - sends lifecycle Slack notifications.

The session-stop flow is deliberately one-way:

```text
strategy + ledger read + active positions
  -> TradingEngine (threshold check and one critical-notifier attempt)
  -> TradingSessionResult (pure application value)
  -> main.py (post-market routing and process exit)
```

A kill-switch result records unresolved position codes but does not call an order or mutate the ledger. The concrete
notifier remains an external adapter: `CALL_RETURNED` means only that its callable returned, not that Slack delivery was
confirmed.

Both functions accept factories/callables so tests can validate ordering without real network, Slack, S3, file deletion,
or trading side effects.

## SQLite ownership and lifecycle

The current SQLite compatibility adapter has one process owner:

```text
Settings.database.path
  -> TradeLogger
       ├─ main SQLite connection: paper ledger reads/writes
       └─ one queue worker connection: physical-state upserts
  -> AsyncPhysicalStateRepository: synchronous queue submission wrapper
  -> TradingEngine: injected ledger and physical repository
  -> close: stop new work -> wait evaluation executor -> close wrapper
            -> append FIFO sentinel -> drain/stop/join worker -> close both connections
  -> post-market readers: open the same configured path -> copy rows -> close
                          -> only then call minute API/pandas/write CSV
```

`TradeLogger.flush()` waits for all accepted physical-state tasks and surfaces the first persistence failure.
`TradeLogger.close()` first rejects new submissions and appends one sentinel behind all accepted FIFO work. Joining that
worker drains the accepted work before it stops, after which both SQLite connections are closed. Cleanup phases continue
after `BaseException`; a bounded second pass recovers one-shot phase failures before terminal state is declared. An
enqueue-after-success interruption can leave the sentinel result unknown; after the worker is dead, the owner atomically
checks the remaining queue and consumes it only when every item is the identical sentinel control object. A remaining
physical task is never discarded and keeps the close explicitly incomplete. `TradeLogger.is_closed` reports terminal
worker, empty queue, and both closed connections under its lifecycle lock.

`TradingEngine.close()` permanently rejects new work on its first call but tracks owned-resource terminal state
separately. Executor and physical-adapter steps are attempted once; only a ledger that neither returns from `close()` nor
reports concrete `is_closed=True` remains pending for a later engine close. Ordinary cleanup failures become an explicit
lifecycle error; `KeyboardInterrupt` and `SystemExit` remain process-control exceptions after cleanup. Owner, concurrent
waiter, and repeated close observe the first latched failure without repeating completed side effects. The process entry
point still attempts close once and fails closed; it does not loop automatically on a persistent resource failure.

The default relative `trades.db` constructor remains only for one compatibility window for direct legacy engine/logger
callers and emits a warning on the direct engine path. Production composition and post-market readers never use that
fallback. A new session requires a new runtime graph; concurrent engines must not share this SQLite writer.

## Application ports

- `MarketDataGateway`
  - used by `MarketDataCollector`, `MarketAnalyzer`, and `StockManager`;
  - prevents those modules from depending on the full Kiwoom client shape.
- `PhysicalStateRepository`
  - used by `PhysicalStateTracker`;
  - moves queue submission and close ownership behind `AsyncPhysicalStateRepository` without an extra executor/event
    loop.
- `PaperTradeLedger`
  - exposes only the engine/manager ledger operations plus `flush()` and `close()`;
  - keeps business consumers independent from SQLite connection details while the concrete compatibility adapter remains
    `TradeLogger`.
- `ArchiveStore` and `ScopedCleanup`
  - keep application archive policy independent of boto3 and filesystem deletion;
  - exchange frozen, filesystem-identity-bound `ArchiveReceipt`, `CleanupReceipt`, and `PostMarketResult` values;
  - distinguish cleanup that never started from completed, partial, and unknown-after-attempt outcomes;
  - make missing, empty, partial, failed, and unsafe-cleanup outcomes explicit.

These are structural `Protocol` contracts. No framework-level dependency injection library is used.

## External boundaries

| Boundary | Concrete code | Isolation status |
| --- | --- | --- |
| Kiwoom REST/auth | `kiwoom_stock.api.*` | concrete service passed as `MarketDataGateway` where possible |
| SQLite ledger/state | `kiwoom_stock.core.database` | configured single-path owner; ledger and physical lifecycle behind application ports; concrete class remains compatibility code |
| Slack messages/uploads | `kiwoom_stock.monitoring.notifier`, `DailyReporter` | lifecycle messages injectable; reporter internals still legacy |
| S3 upload | `kiwoom_stock.utils.s3_manager` | injected client seam; no-follow descriptor upload; typed per-target identity receipt |
| Filesystem cleanup/output | `kiwoom_stock.utils.file_manager`, tools | production cleanup pins root/date descriptors and rechecks archived inode/metadata before descriptor-relative unlink; local retention and tools still perform real file I/O by design |
| Clock/market calendar | startup uses an injected date provider and explicit-date XKRX adapter; engine/other consumers still use system time | partially isolated |
| Gemini | `kiwoom_stock.utils.gemini_client` | still legacy; not called in tests |

## Persistence invariants

B6 changes ownership and lifecycle, not business data meaning. The `trades` and `physics_state` schemas, column order,
`OPEN` default, `CLOSED` transition, physical upsert key, timestamps, row shapes, buy/sell values, and percentage PnL
formula remain characterized. The kill switch creates no broker or paper order, performs no ledger mutation, preserves
active positions, closes persistence resources, and skips post-market work.

## Known remaining architecture debt

- `TradeLogger` still combines ledger and physical-state SQLite concerns; the single queue worker is now explicit and
  lifecycle-managed, but the compatibility class has not been split into backend-specific adapters.
- The direct `TradingEngine(client, config)` compatibility fallback can still create cwd `trades.db`; production runtime
  tests prove it is unused, and it should be removed after legacy callers migrate.
- `DailyReporter` still imports standalone tools and Slack upload behavior directly.
- `KiwoomClient.__init__()` is network-lazy. The enabled production runtime
  explicitly calls `ensure_auth_ready()` once after local resource/client
  construction and before engine construction; disabled and holiday/config-only
  paths remain network-free.
- Real order execution is still marked TBD; no production execution gateway has been introduced.
- Compose supports only one application process/replica owning a local named SQLite volume. Multiple processes,
  replicas, hosts, or shared/network storage require a PostgreSQL-class backend and a separately planned migration.
- The application has no SIGTERM-to-session-shutdown adapter. Docker stop grace and `STOPSIGNAL` declarations are not
  evidence that the queue drains under an actual container stop.
- Docker runtime checks require an explicitly approved Docker/daemon execution; this change has only static Compose/YAML
  evidence and does not claim build, render, start, or stop behavior.
