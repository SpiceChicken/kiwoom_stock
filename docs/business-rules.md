# Business rules

This document summarizes the rules currently protected by tests. It is not a trading recommendation.

## State and physics rules

- Initial monitored velocity is seeded from RSI: `max(0, (RSI - 50) / 10)`.
- Volume freeze is detected when the total volume is unchanged and non-negative.
- Frozen ticks:
  - set strength to `0.0`;
  - set volume ratio to `0.0`;
  - set interval impulse to `0.0`;
  - skip physical-state persistence.
- Volume acceleration uses a 120-tick window:
  - latest 60 ticks are compared with the previous 60 ticks;
  - missing previous window defaults the drop ratio to `1.0`.
- Reference mass:
  - below or equal to 100B KRW market cap: `10_000_000`;
  - above that: logarithmic scale based on market cap.
- Interval impulse:
  - computed from interval volume times current price;
  - only applies when not frozen, interval volume is positive, and interval amount reaches reference mass.
- Crash recovery:
  - reads last `velocity` and `timestamp`;
  - applies exponential hourly decay.

## Entry rules

`TradingStrategy.evaluate(...)` applies these decision gates in priority order:

1. Zero-price guard: current price `<= 0` blocks buy evaluation.
2. Climax shield: high gravity with high thrust blocks suspected blow-off/sell-off setups.
3. Breakout override: strong impulse and jerk can override later hard locks.
4. Hard locks:
   - insufficient thrust;
   - negative net force;
   - high-altitude stall;
   - volume exhaustion.
5. Standard entry: positive acceleration/jerk after all locks pass.

Boundary behavior is covered by characterization tests, including inclusive threshold cases.

## Exit and retention rules

- Zero-price positions are not panic-sold.
- Paper positions use the string-compatible lifecycle states `OPEN`, `OVERNIGHT`, and `CLOSED`.
- Fresh buys persist the current XKRX session date and an aware KST state-change instant. An overnight
  candidate is committed as `OPEN -> OVERNIGHT` before memory changes; strategy evaluation returns a typed
  intent and does not mutate the position.
- An already persisted `OVERNIGHT` position is not reconsidered in the same owner session. During a later
  regular XKRX session it is reopened exactly once, with the observed session as its new owner, before
  target/stop or other exit decisions run. Holidays and pre/post-market instants do not cause transitions.
- `OVERNIGHT -> CLOSED` is not a valid direct transition. Ledger, manager, and engine require a committed
  `OVERNIGHT -> OPEN` reconciliation receipt before any sell decision can close the row.
- Active legacy `OPEN` rows are backfilled only from an exact KST `buy_time` that belongs to an XKRX session.
  All active identity, name/regime/time, price, force, status, and lifecycle fields are decoded before any
  position memory is published. Missing legacy `OVERNIGHT` metadata, duplicates, and malformed active data
  fail closed instead of being guessed.
- Fixed paper exits use the versioned `percentage-points-v1` unit and full-precision raw position return:
  - target is inclusive at `profit_rate >= target_profit_percentage_points`;
  - stop is inclusive at `profit_rate <= -stop_loss_percentage_points`;
  - fixed target/stop is checked before a new late-session overnight candidate, forced day close, and dynamic exits.
- `TargetStopPolicy` and the unrounded position-return calculation are pure domain contracts. Settings constructs the
  policy once and runtime/engine forwards the same object to the strategy.
- `Position.calc_profit_rate`, fixed exits, and `TradeLogger.record_sell` consume the same unrounded percentage-point
  calculation. SQLite stores the raw float; two-decimal formatting remains a notifier/report display concern only.
- Fixed exit reasons state the `%p` threshold and unit. They are local paper-ledger decisions and do not claim a
  broker fill or account result.
- Day-trade exit time remains configurable.
- `cumulative_trade_return_score` is the unweighted simple sum of CLOSED per-trade percentage-point returns for an
  explicit XKRX session date and active `Position.calc_profit_rate` current marks. Quantity, notional, fees, tax,
  currency, and weighting do not participate.
- Kill switch activates inclusively when `cumulative_trade_return_score <= cumulative_trade_return_score_floor`.
  Both values must be finite non-boolean numbers and the floor must be at or below zero.
- A confirmed kill switch is a terminal safety stop, not a liquidation receipt:
  - no actual or paper buy/sell order is created;
  - active position objects and ledger state remain unchanged and their codes are returned as an immutable unresolved
    snapshot;
  - the critical notifier callable is attempted once, with no critical retry or generic error-notification fallback;
  - terminal evidence and the critical message state the cumulative trade return score and floor in percentage-points;
  - the engine latches the result, so the current and repeated `run()` calls perform no further tick, evaluation, order,
    database-write, or scheduler-sleep work.
- Profit lock-in starts once profit reaches the configured threshold and combines velocity loss with a protected profit floor.

## Operational rules

- Startup validates all settings before reading the startup date or checking the KRX calendar. Missing or invalid
  settings exit `1` even on a holiday and do not create output, file logs, clients, workers, or external calls.
- After valid settings, one system-local startup date is used for both the explicit-date KRX calendar and dated output.
  A valid holiday exits `0` without publishing runtime compatibility settings or activating the runtime graph.
- The local calendar adapter retains its conservative legacy policy: a calendar-library exception is logged and treated
  as closed/exit `0`. Operators must not treat that result alone as evidence of a real exchange holiday.
- A kill-switch session result skips reporting, S3 archive, local cleanup, and the normal finish notice, then terminates
  the process with exit code `1`. Market-close and engine-caught user-interrupt results retain normal post-market handling
  and exit code `0`.
- Post-market behavior:
  - local/non-prod: run daily post-mortem, skip S3, keep output for three days;
  - `prod` and `production-like`: a bucket and at least one matching direct daily CSV are required for archive success;
  - production-class cleanup runs only when every required target has a successful, immutable source-identity receipt;
  - cleanup may unlink only identity-matching direct CSV entries through the pinned matching `YYYYMMDD` directory;
  - missing configuration/source/targets, partial or total upload failure, adapter error, or unsafe scope keeps cleanup
    closed and requires attention;
  - a scope/identity rejection proven before cleanup is `NOT_STARTED` and preserves outputs, while an unexpected adapter
    exception after cleanup invocation is `UNKNOWN_AFTER_ATTEMPT` and must never claim that all outputs were preserved.
- Tests must use fake/mock/stub boundaries and must not call Kiwoom, orders, Slack, S3, Gemini, or production DB paths.
