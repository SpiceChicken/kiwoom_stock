# Configuration

`kiwoom_stock.settings` is the single runtime settings boundary. Application and domain code receive frozen
standard-library dataclasses and must not read environment variables or import Pydantic directly.

## Startup and source precedence

Process startup validates the complete typed settings snapshot before it reads the system date or asks the KRX calendar.
Missing or invalid settings therefore exit with status 1 on both trading days and holidays, list every invalid canonical
variable, and do not create output, log files, clients, engines, workers, or network connections. A valid holiday exits
with status 0 without publishing compatibility mappings or creating those runtime artifacts.

Validation and runtime activation are separate operations. `validate_environment_settings()` reads legacy, process
environment, and configured secret sources but does not mutate the compatibility globals or filesystem.
`activate_runtime_settings(settings, date)` creates the dated output directory first and publishes the same frozen
settings snapshot only after that succeeds. The composition root forwards one startup date to the explicit-date KRX
calendar and the open-day runtime, so calendar and output selection cannot read different dates at midnight.

Non-secret settings come from canonical `KIWOOM_*` process environment
variables, explicitly loaded legacy mappings, and documented defaults.
`KIWOOM_APP_KEY`, `KIWOOM_SECRET_KEY`, and `KIWOOM_BASE_URL` are forbidden in
the process environment and every legacy mapping, including case/separator
aliases nested inside JSON sequences. In `mock` or `prod` mode,
the strict POSIX provider reads the two credential files from the absolute,
repository-external `KIWOOM_CREDENTIALS_DIR`.

Endpoint selection is not configurable: `mock` always derives
`https://mockapi.kiwoom.com` and is accepted only for
`KIWOOM_APP_ENV=staging`; `prod` always derives
`https://api.kiwoom.com` and is accepted only for `prod` or
`production-like`. Disabled mode remains valid in every application
environment and rejects a stale credential directory.

Canonical values override legacy values. Legacy use, duplicates, ignored JSON resources, unknown legacy keys, and
unused `SCORING_CONFIG` are exposed as migration warnings. Conflicting legacy values fail validation unless a
canonical value explicitly resolves that setting. Optional blank values in `.env.example` mean disabled/unset and
also prevent deprecated legacy values from being reactivated. Secret values are excluded from typed settings errors,
diagnostics, and reprs; do not log raw legacy compatibility mappings because they still carry consumer-facing values.

## Canonical settings matrix

The following table is checked against the machine-readable `SETTING_SPECS` registry and `.env.example`.

<!-- settings-matrix:start -->
| Name | Type | Required | Default | Consumer | Sensitive | Environments | Validation |
|---|---|---|---|---|---:|---|---|
| `KIWOOM_API_MODE` | enum | no | `disabled` | runtime composition | no | all | `disabled`, `mock`, or `prod` |
| `KIWOOM_PROCESS_NAME` | string | yes | none | runtime lifecycle | no | all | non-empty |
| `KIWOOM_APP_ENV` | enum | no | `local` | retention policy | no | all | allowed environment |
| `KIWOOM_CREDENTIALS_DIR` | absolute directory path | for mock/prod | none | strict credential provider | no | staging/prod-like | absolute external directory |
| `KIWOOM_OUTPUT_DIR` | directory path | no | current working directory | reports | no | all | safe non-root path |
| `KIWOOM_DB_PATH` | file path | no | `trades.db` | runtime and post-market SQLite | no | all | safe non-root path |
| `KIWOOM_SLACK_WEBHOOK_URL` | URL | no | none | Slack webhook | yes | all | HTTP(S) URL with host |
| `KIWOOM_SLACK_BOT_TOKEN` | string | with channel | none | Slack upload | yes | all | non-empty pair |
| `KIWOOM_SLACK_CHANNEL_ID` | string | with token | none | Slack upload | no | all | non-empty pair |
| `KIWOOM_GEMINI_API_KEY` | string | no | none | Gemini reports | yes | all | non-empty when set |
| `KIWOOM_S3_BUCKET_NAME` | string | no; production-class missing preserves outputs | none | S3 archive | no | prod/prod-like | valid bucket when set |
| `KIWOOM_AWS_REGION` | string | no | SDK/provider | future AWS session | no | staging/prod-like | lowercase region |
| `KIWOOM_FAST_INTERVAL_SECONDS` | positive float | no | `10` | TradingEngine | no | all | greater than 0, at most slow |
| `KIWOOM_SLOW_INTERVAL_SECONDS` | positive float | no | `60` | TradingEngine | no | all | greater than 0, at least fast |
| `KIWOOM_MAX_WORKERS` | positive integer | no | `8` | TradingEngine | no | all | greater than 0 |
| `KIWOOM_MARKET_PROXY_CODE` | six-digit string | no | `069500` | MarketAnalyzer | no | all | exactly six digits |
| `KIWOOM_MAX_STOCKS` | positive integer | no | `50` | StockManager | no | all | greater than 0 |
| `KIWOOM_ETF_KEYWORDS` | CSV strings | no | empty | StockManager | no | all | unique non-empty items |
| `KIWOOM_DEBUG_MODE` | strict boolean | no | `false` | TradingStrategy | no | all | only `true` or `false` |
| `KIWOOM_DAY_TRADE_EXIT_TIME` | HH:MM | no | `15:30` | TradingStrategy | no | all | valid 24-hour time |
| `KIWOOM_ENTRY_DEADLINE` | HH:MM | no | `15:00` | TradingStrategy | no | all | earlier than exit time |
| `KIWOOM_TOTAL_LOSS_LIMIT` | float percent | no | `-5` | TradingStrategy | no | all | finite and at most 0 |
<!-- settings-matrix:end -->

`KIWOOM_S3_BUCKET_NAME` remains optional. In both `prod` and `production-like`, an unset bucket is an explicit
`NOT_CONFIGURED` archive outcome: no S3 client or cleanup is started, local outputs are preserved, and the returned
post-market result requires attention. Operators must configure and validate a bucket before first production archive
activation.

## SQLite path contract

`KIWOOM_DB_PATH` is consumed by both runtime composition and post-market report readers. Runtime constructs exactly one
`TradeLogger` with `Settings.database.path`; its ledger connection and physical-state queue worker connection use the
same normalized file. After the engine closes, each report reader opens that configured path, copies the required rows
into memory, closes the DB exactly once, and only then performs API, pandas, or CSV work.

The default `trades.db` value exists for direct legacy/local compatibility. It is relative to the process working
directory and must not be relied on in managed environments. Compose pins the explicit value
`/var/lib/kiwoom/trades.db`, backed by the `kiwoom-data` named volume at `/var/lib/kiwoom`. The directory must already
exist and be writable by UID/GID `10001:10001`; settings validation does not create a missing DB parent or repair volume
ownership. Do not hide a permission or missing-parent failure with a cwd fallback.

SQLite deployment supports one application process, one replica, one writer owner, and local storage only. Do not use
Compose CLI scaling or another supervisor instance against the same file. Multiple replicas/processes, multi-host or
network storage, sustained lock contention, or a need for online migrations/HA is the trigger to plan a PostgreSQL-class
backend instead of extending this SQLite lifecycle.

## Local and container usage

For local development, use Python 3.11 or newer. Python 3.14 is the primary/latest development and CI runtime. Create
a virtual environment, install with `python -m pip install -e ".[dev]"`, and export values based on `.env.example`.
The application does not implicitly load `.env`; the shell, Compose, or a process manager must inject it.

For mock/prod credential files, set `KIWOOM_CREDENTIALS_DIR` to a dedicated
absolute directory outside the repository. Files named exactly
`KIWOOM_APP_KEY` and `KIWOOM_SECRET_KEY` are opened descriptor-relatively and
must satisfy the strict owner/mode/link/content contract. Disabled mode rejects
a stale credential directory. Compose file-backed secret UID/mode remapping is
not trusted; activation remains blocked until UID 10001 metadata/readability is
verified on the actual target.

Credential loading captures the directory identity at provider construction,
opens both files descriptor-relatively, and compares directory and file
generations before returning the pair. Intermediate/final symlinks,
constructor-to-load directory replacement, in-read file replacement, and
partial pair rotation fail closed. Rotation is restart-based: keep the worker
DISARMED, create both files in a new hardened external directory, switch the
directory reference, then start a new process. Never update one live file at a
time.

Compose `${...:?}` expressions only prove that a host variable was supplied.
They cannot prove an absolute path, and an in-container provider cannot prove
that a mounted source was outside the host checkout. A host launcher/validator
for these two properties is not implemented, so mock/prod container activation
is explicitly BLOCKED until that validation and the target metadata smoke are
available.

The security policy, permitted delivery boundary, inventory fields, and
rotation procedure are defined in
[Kiwoom credential management](security/kiwoom-credential-management.md) and
[credential rotation](operations/credential-rotation.md). These documents
never serve as a place to submit credential values.

Legacy JSON remains a temporary compatibility source. Current consumers still receive read-only `CONFIG` and
`STRATEGY_CONFIG` views after explicit startup wiring. Importing `core.config` does not read JSON, inspect the clock,
create directories, or read environment variables. Standalone report tools explicitly configure these views, consume
the returned typed database path, and close their short-lived reader before later report I/O. Other legacy mapping
consumers remain scheduled for later migration.
