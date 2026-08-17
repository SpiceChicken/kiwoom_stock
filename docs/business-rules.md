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

## G0 swing candidate contract (normative SSOT; inactive)

The following is a characterization/contract boundary for an isolated candidate that remains disabled by default. It
does not change, enable, or reinterpret any legacy shadow-paper rule above. Candidate runtime/evidence wiring exists
behind an explicit opt-in and immutable strategy-context provider; the bounded composition calls the pure
`evaluate_swing()` evaluator and records its typed decision alongside input/output hashes. A missing context provider
or a candidate state/position/episode-identity mismatch fails closed. Candidate state hydration is read-only, and no broker execution
is available. This section is the only
normative source for the inactive candidate contract;
configuration documentation must reference it and must not copy its terms. Planner shorthand is non-normative when
it differs from this section. The exact UTF-8 text between the canonical markers below is the policy text to hash.

<!-- G0-POLICY-CANONICAL-BEGIN -->
1. Session counting: `session_date` is an XKRX regular-session date and `holding_session_number = 1 +` the number
   of actual XKRX session ordinals after entry; entry is 1, thesis/time exits are `>= 2`, and time-exit eligibility
   is `>= 20`. Hard-risk is the session-1 exception; calendar-day arithmetic is invalid.
2. Hard-risk: the only allowlisted reasons are exactly `CATASTROPHIC_PRICE_RISK` and `PORTFOLIO_RISK_LIMIT`.
   Either reason requires both a raw executable price and a versioned risk threshold. Missing marks and accounting
   errors block new entry and are never liquidation evidence or a synthesized liquidation.
3. Fill timing: admission uses a completed D-1 slow context and a completed fast bar; the next eligible XKRX
   regular-session bar open after the decision is the all-or-none fill point and `decision_at <= fill_at`. An
   unfilled or rejected admission has no fill and no cash movement, and consumes the episode.
4. Lot policy: one active lot per portfolio and symbol, with positive integer quantity; pyramiding and partial fills
   are not allowed.
5. Mark policy: mark quality is exactly `OFFICIAL_CLOSE`, `PROVISIONAL_LAST_VALID_REGULAR`,
   `SUSPENDED_CARRY_FORWARD`, or `MISSING`. Every mark stores `source_id`, `available_at`, `computed_at`,
   `revision`, and `supersedes_id`. `OFFICIAL_CLOSE` is canonical; a provisional regular mark is revised by the
   subsequent official close with `supersedes_id` linking the prior revision. `SUSPENDED_CARRY_FORWARD` and
   `MISSING` are `INCOMPLETE` and block new entries. `MISSING` never creates a zero-KRW mark or liquidation.
6. Cash and flow: initial cash is fixed, external flow is exactly `0`, and accounting is trade-date accounting.
   Gross, base, and stress cost views are versioned; the official rate remains `TBD` here.
7. Cost policy: raw executable price is authoritative for execution and accounting; adjusted price is feature-only.
   Price, cash, and cost are integer KRW; ratios are Decimal strings or bps, never an implicit binary float contract.
8. Corporate actions: a known action is usable only when its point-in-time `available_at <= decision_at` and it is
   applied at the effective XKRX session boundary as one atomic quantity, cost-basis, and cash event. An unknown
   action is `INSUFFICIENT_DATA`, is not applied, and blocks fill/NAV acceptance and new entry; it is never estimated
   after the fact and cannot produce a terminal value.
9. Episode lifecycle: `ARMED -> ACTIVE -> CONSUMED -> COOLDOWN -> ARMED`; activation requires a false-to-true
   rising edge and an episode cannot re-arm unless the position is flat. Exit or rejection enters `COOLDOWN`; a
   persistent signal does not re-arm. Re-arm requires a false slow predicate, two completed fast bars that are false,
   and one XKRX cooldown session. A strategy semantic-version change is a terminal event.
10. Legacy unknowns: unknown corporate action or missing legacy quantity, cost, episode, horizon, or mark is exactly
    `INSUFFICIENT_DATA`; no value may be inferred. Every instant is aware KST or canonical wire time.
11. Temporal evidence: the entry ordinal is 1, thesis/time candidates require ordinal `>= 2`, and a time candidate
    requires ordinal `>= 20`. Only completed D-1 slow context and a completed fast bar may inform a decision; a fill
    cannot precede its decision, and only the next eligible regular-session bar open may fill it.
<!-- G0-POLICY-CANONICAL-END -->

## Offline swing economics evaluation boundary

- Economic evaluation aggregates by `episode_id`, not by individual tick or signal. One episode may contain one
  complete entry lot and at most one complete exit; repeated same-symbol entries remain separate episodes and are
  not silently netted together.
- Gross, base, and stress cash deltas are kept separately. Base/stress cost is derived from typed accounting output,
  and stress cost cannot be lower than base cost.
- A closed episode reports realized after-cost P&L. An open episode requires a complete identity-bound mark and
  reports marked unrealized after-cost P&L; missing or incomplete marks fail closed.
- Comparison evidence reports episode count, fill count, base/stress P&L, cost totals, loss-episode count, and
  average holding sessions. A comparison is valid only when baseline and candidate use the same dataset identity.
- This evaluator is offline research evidence only. It does not create orders, mutate the ledger, call a broker, or
  claim that marked/unrealized P&L is a realized account result.

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
