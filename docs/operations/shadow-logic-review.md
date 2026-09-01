# Shadow business-logic review

This runbook reviews the complete market-read -> strategy -> isolated paper
ledger decision path without granting account, broker-order, cancel, revoke,
report, archive, or application-notification capabilities. A BUY or SELL is a
local paper transition only. Its fill price is the current market tick used by
the verdict; no broker fill is requested.

## Evidence boundary

Each accepted shadow cycle performs exactly six HTTP attempts: one OAuth token
request and five market-data reads for Samsung Electronics (`005930`) and the
KOSPI 200 ETF proxy (`069500`). A fresh one-shot runtime owns each cycle and
closes its market client, HTTP session, engine, and database handles before the
next cycle.

The cycle evidence includes categorical `decision_telemetry`. It intentionally
does not include prices, webhook values, credentials, account data, arbitrary
strategy text, or raw API payloads.

The telemetry fields are:

- `market_regime`: the proxy-chart regime (`STABLE_BULL`, `VOLATILE_BULL`,
  `QUIET_BEAR`, `PANIC_BEAR`, or `NEUTRAL`).
- `strategy_reason_code`: the exact stage that admitted or blocked entry.
- `strategy_intent`: `ENTRY_SIGNAL` or `NO_ENTRY_SIGNAL`.
- `paper_action`: the isolated ledger transition (`BUY`, `SELL`, or `HOLD`).
- `position_before`: `FLAT`, `OPEN`, or `OVERNIGHT` after session reconciliation
  and before the decision.
- `session_phase`: `ENTRY`, `EXIT_ONLY`, or `CLOSED`.
- `net_force_band`, `current_velocity_band`, and `jerk_band`: categorical
  direction evidence for the physics model.
- `thrust_band`: the exact entry-rule range (`<0.8`, `0.8–<1.0`,
  `1.0–<1.5`, or `>=1.5`) used to independently verify thrust locks.
- `strength_band`, `trend_rsi_band`, and `price_vwap_relation`: categorical
  context for supply strength, momentum, and price location.

The application model and the independent host validator both reject
cross-field contradictions. Examples include a BUY outside the entry phase, a
SELL without an open paper position, an entry reason with non-positive jerk, or
a standard entry with negative net force. `BREAKOUT_OVERRIDE` is the only entry
reason allowed to bypass the net-force lock; it still requires positive jerk.

## Intraday review gates

An intraday sample is accepted only when all of these are proven by the same
immutable source/image/activation tuple:

1. `runtime_status=PASS`, six exact HTTP attempts, and exact per-endpoint counts.
2. Account, broker order, OAuth revoke, Slack/Gemini, S3, and reports remain
   false in runtime side-effect evidence.
3. `resources_closed=true` for every cycle.
4. A multi-cycle run has a minimum start interval of at least 60 seconds and
   `db_reopens == cycles - 1`.
5. Every decision telemetry object passes both categorical and cross-field
   validation.
6. A protected stop produces `STOPPED/stop-requested` or the fixed 7-hour cap
   produces `DEADLINE/run-deadline`, followed by exact container removal.

Review at least these session phases separately:

- Morning `ENTRY`: regime and entry locks under normal liquidity.
- Afternoon `ENTRY`: continuity after repeated database reopen and refreshed
  market snapshots.
- `EXIT_ONLY`: no new paper BUY; an OPEN paper position may HOLD, SELL, or be
  marked overnight according to the strategy clock.
- Friday close: after the entry deadline and around the forced-exit boundary,
  verify that no BUY occurs and that an OPEN day-trade position receives the
  expected paper-only close transition. After the monitoring window, expect
  `session_phase=CLOSED` and no BUY.

## Re-review matrix

For each accepted artifact, compare the following relationships rather than a
single isolated signal:

| Observation | Required interpretation |
| --- | --- |
| `ENTRY_SIGNAL` | Reason is breakout, uptrend, or reversal and jerk is positive |
| Standard entry | Net force is neutral or positive |
| `BREAKOUT_OVERRIDE` | May have negative net force; all other entry invariants remain true |
| `NO_ENTRY_SIGNAL` | Reason code identifies the first blocking stage |
| `paper_action=BUY` | Flat before decision and `session_phase=ENTRY` |
| `paper_action=SELL` | Open before decision; transition remains local DB only |
| `session_phase=EXIT_ONLY/CLOSED` | No paper BUY |
| Second or later cycle | Same DB identity, reopened handle, minimum 60-second cadence |

One run is operational evidence, not profitability evidence. Strategy-quality
claims require multiple bounded samples across different regimes and a
post-close replay/aggregation of the categorical artifacts. No live-trading
promotion follows from these results.
