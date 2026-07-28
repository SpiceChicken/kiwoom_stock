# Kiwoom production read-only validation

This validator is an explicitly confirmed production smoke test for the
existing market-analysis path. It authenticates and performs only five
allowlisted market reads. It cannot construct an account or order service,
revoke a token, write a database, send a report, or notify an external system.
It is not an activation or order-capability test.

## Preconditions

- Keep every worker and order capability `DISARMED`.
- Use a repository-external credential directory accepted by
  `StrictFileCredentialProvider`. Never pass key material as command-line
  arguments or environment variables.
- Obtain the operational approval required for production OAuth and market
  reads. The confirmation flag records operator intent; it does not grant
  approval.
- Confirm the target stock code. The fixed regime proxy is KODEX 200
  (`069500`).

No production, AWS, secret-provider, or Kiwoom call was made while building or
testing this command. Its first approved live execution remains an operational
validation step.

## Run

Install the project with its normal dependency unit, then run:

```bash
kiwoom-live-readonly-validate \
  --credentials-dir /absolute/repository-external/kiwoom-credentials \
  --stock-code 005930 \
  --confirm-prod-read-only
```

`tools/validate_live_kiwoom_readonly.py` is a compatibility wrapper around the
installed entry point. Omitting `--confirm-prod-read-only` fails before opening
the credential provider.

The regime proxy is fixed to KODEX 200 (`069500`) and is not a CLI option. The
minute intervals are also not configurable: the target stock is fetched at
five minutes and the regime proxy at 60 minutes. The shared HTTP session admits
only these exact path/API-ID pairs:

| Purpose | Path | API ID |
| --- | --- | --- |
| OAuth token | `/oauth2/token` | `au10001` |
| Stock basic | `/api/dostk/stkinfo` | `ka10001` |
| Stock/proxy chart | `/api/dostk/chart` | `ka10080` |
| Tick strength | `/api/dostk/mrkcond` | `ka10046` |
| Order book | `/api/dostk/mrkcond` | `ka10004` |

The chart payload boundary admits only the selected stock at five minutes and
the selected proxy at 60 minutes. Every other origin, method, path/API-ID pair,
stock, interval, payload shape, account, order, or revoke attempt fails closed.
All attempts, including retries and 401 refreshes, share a 23-attempt ceiling.

## PASS contract

PASS requires all of the following:

- exact logical read order: stock basic, stock five-minute chart, proxy
  60-minute chart, strength, order book;
- basic `cur_prc`, `trde_pre`, `trde_qty`, and `mac` are present and finite;
  directional price/quantity fields have positive magnitude and `mac` is
  positive;
- both charts contain at least 14 rows, every consumed price field has a
  finite positive absolute value (the source may carry a direction sign), and
  every consumed volume is finite and nonnegative;
- strength contains at least five rows and rows zero and four have finite,
  positive `cntr_str`;
- both order-book totals are finite and nonnegative, with at least one side
  positive;
- `update_regime()` produces a non-`UNKNOWN` regime;
- every required analyzer metric is positive and finite;
- the force map contains exactly the expected finite force keys;
- the in-memory physical-state repository receives exactly one submission;
- a deterministic-clock `TradingStrategy` receives the analyzer regime and
  metrics, and returns a typed verdict whose regime matches the analyzer;
- every allowlisted API is attempted and the HTTP budget is respected.

The raw numeric parser accepts plain JSON numbers and strings with correctly
grouped commas and at most one leading `+` or `-`. Direction-signed prices and
quantities are validated by magnitude in line with the existing Kiwoom parser.
Whitespace, misplaced commas, embedded signs, exponent notation, booleans,
non-finite values, and every other character fail closed without echoing the
rejected value.

Output is built from an explicit allowlist DTO containing only status, selected
codes and intervals, regime, validated metrics and forces, safe per-API counts,
logical sequence, submission count, the verdict fields `status`,
`is_buy_signal`, and normalized known `regime`, and false side-effect
declarations. Raw verdict extras and its raw force object are not serialized.
Output does not include credentials, bearer tokens, raw responses, exception
text, or secret-derived values. Analyzer and collector errors are redacted
during this run; a BLOCKED result reports only a typed safe error category.

## Artifact smoke-test boundary

The automated wheel test builds the wheel without network dependency
resolution, installs that wheel under a temporary prefix with `--no-deps`, and
uses the current test interpreter's already-installed runtime dependencies. It
asserts that `kiwoom_stock` is imported from the temporary installed wheel path
before executing the installed console script with `--help`.

This proves wheel inclusion and entry-point wiring only. It does not prove a
clean-host installation of the full dependency unit. A clean-host full
dependency installation and the first approved live schema/sample validation
remain BLOCKED follow-up checks in an environment authorized for package
resolution and production market reads.

## Failure handling

Treat any BLOCKED output, missing field, non-finite value, `UNKNOWN` regime,
budget exhaustion, or boundary rejection as `NO_GO`. Do not weaken the
allowlist, increase retries, substitute another chart interval, print the raw
response, or enable account/order capability to diagnose it. Preserve only
redacted output and safe API counts, then investigate offline or in an
explicitly approved staging/live validation window.
