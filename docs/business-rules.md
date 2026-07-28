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
- Day-trade exit time remains configurable.
- Kill switch activates inclusively when total realized plus unrealized PnL is less than or equal to the configured loss
  limit.
- A confirmed kill switch is a terminal safety stop, not a liquidation receipt:
  - no actual or paper buy/sell order is created;
  - active position objects and ledger state remain unchanged and their codes are returned as an immutable unresolved
    snapshot;
  - the critical notifier callable is attempted once, with no critical retry or generic error-notification fallback;
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
