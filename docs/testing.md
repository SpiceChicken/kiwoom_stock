# Testing and build verification

## Python policy

- Supported project minimum: Python 3.11.
- Primary/latest development and CI track: Python 3.14.
- Use `venv` and `pip`; Poetry is not part of this project contract.

## Local verification

Run these commands from the repository root:

```bash
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
python -m flake8 src tests tools main.py --count --select=E9,F63,F7,F82 --show-source --statistics
python -m mypy src/kiwoom_stock
python -m mypy main.py
python -m pytest tests
python -m build
env -u KIWOOM_APP_KEY -u KIWOOM_SECRET_KEY -u KIWOOM_BASE_URL \
  KIWOOM_API_MODE=disabled KIWOOM_PROCESS_NAME=paper-monitor \
  python -m kiwoom_stock --check-config
python - <<'PY'
import kiwoom_stock
from kiwoom_stock.domain import state
from kiwoom_stock.core.state_manager import PhysicalStateTracker

assert kiwoom_stock.__name__ == "kiwoom_stock"
assert state.calculate_initial_velocity_from_rsi(65.0) == 1.5
assert PhysicalStateTracker.__name__ == "PhysicalStateTracker"
PY
```

Missing configuration smoke should fail before any external client starts:

```bash
set +e
env -u KIWOOM_APP_KEY -u KIWOOM_SECRET_KEY -u KIWOOM_BASE_URL -u KIWOOM_PROCESS_NAME \
  python -m kiwoom_stock --check-config
status=$?
set -e
test "${status}" -eq 1
```

Raw credential and endpoint environment variables are forbidden even when
empty. Check each name separately without supplying a credential value:

```bash
failure_output="$(mktemp)"
trap 'rm -f "${failure_output}"' EXIT
for forbidden_name in KIWOOM_APP_KEY KIWOOM_SECRET_KEY KIWOOM_BASE_URL; do
  set +e
  env -u KIWOOM_APP_KEY -u KIWOOM_SECRET_KEY -u KIWOOM_BASE_URL \
    KIWOOM_API_MODE=disabled KIWOOM_PROCESS_NAME=paper-monitor \
    "${forbidden_name}=" python -m kiwoom_stock --check-config \
    >"${failure_output}" 2>&1
  status=$?
  set -e
  test "${status}" -eq 1
  grep -q "${forbidden_name}" "${failure_output}"
done
```

## Startup fail-fast contract

`tests/test_main_startup.py` verifies the process boundary without constructing a real client or worker:

- missing or invalid settings aggregate canonical names and help, redact submitted secrets, and exit `1` before the
  date provider or calendar runs;
- an unexpected settings-source error retains the fatal exit-`1` policy but does not attempt a crash notification
  before a monitor exists;
- valid holiday startup follows `validate -> date -> calendar -> exit 0` with no compatibility publish, output, file
  logs, database, client, thread, network, or post-market work;
- valid open startup forwards the same frozen `Settings` identity and single date into runtime activation;
- fixed-date XKRX checks, exception-to-closed behavior, source entrypoint subprocesses, and import-only subprocesses are
  deterministic and side-effect guarded.

The targeted local command is:

```bash
python -m pytest \
  tests/test_settings.py \
  tests/test_runtime_composition.py \
  tests/test_main_startup.py \
  tests/test_main_session.py -q
```

## SQLite path and shutdown contract

The B6 suite uses only SQLite files below pytest temporary directories. It verifies:

- runtime forwards the exact typed database path before client construction and injects one ledger/physical adapter
  identity into the engine;
- ledger and physical rows use one configured file and no cwd `trades.db` appears;
- a FIFO sentinel drains accepted physical-state tasks before the worker exits, both connections close, and repeated
  `flush()`/`close()` is side-effect idempotent;
- deterministic queue/sentinel/thread/main-connection/worker-connection failures continue later cleanup phases, recover
  one-shot failures in a bounded pass, retain persistent incomplete state for retry, and re-surface ordinary or
  process-control failures to owner, concurrent waiter, and repeated callers;
- enqueue-after-success `KeyboardInterrupt` and `SystemExit` with a gated real worker preserve the accepted row, consume
  orphan control-only sentinels, leave the worker dead/queue empty/connections closed, and surface the same failure to
  owner, concurrent waiter, and repeated caller;
- a dead-worker queue containing any physical task is left intact and explicitly incomplete rather than discarded;
- runtime construction rollback and engine shutdown attempt every owned cleanup step while preserving the primary error;
- engine close permanently rejects work, never repeats completed executor/physical steps, retries only a genuinely
  incomplete ledger, and treats a concrete ledger `is_closed=True` as terminal even when it re-raises historical failure;
- main closes the engine exactly once before post-market work, kill routing, or error exit;
- report readers copy rows and close the configured DB before minute fetch, pandas, or CSV output;
- schema, row shape, `OPEN`/`CLOSED`, percentage PnL, and kill-switch no-order/no-ledger-mutation behavior remain
  characterized.

Run the focused persistence/session/report suite with:

```bash
python -m pytest -q \
  tests/test_database_lifecycle.py \
  tests/test_physical_state_repository.py \
  tests/characterization/test_paper_ledger_characterization.py \
  tests/test_runtime_composition.py \
  tests/test_engine.py \
  tests/test_main_session.py \
  tests/test_main_startup.py \
  tests/test_settings.py \
  tests/test_package_acceptance.py
```

## No-external policy

Tests and CI must not call real Kiwoom APIs, place orders, send Slack messages, upload to S3, call Gemini, or write
operational databases. Use fakes, mocks, stubs, temp directories, and injected clocks.

The post-market archive suite injects a fake S3 client and uses only `tmp_path` files. It covers missing/empty/partial/
failed archive receipts with real boto managed exception classes, invalid commands before upload, no-follow source
handling, full-success cleanup, traversal/sibling/nested/wrong-date/duplicate/symlink rejection, upload target and
parent replacement races, uploaded-file identity replacement, validation atomicity, unknown-after-attempt state, and
per-path unlink failure. Real AWS/IAM/bucket behavior remains an explicit pre-deployment validation gap; local fake
tests do not close it.

Local SQLite/thread tests likewise do not prove named-volume ownership, host permissions, real supervisor signals, or
container stop behavior. No test may use an operational DB file. An actual Docker/SIGTERM validation remains RED until a
Docker-enabled isolated environment is explicitly approved.

## Container static contract

Without starting Docker, the repository can validate YAML shape, the exact configured SQLite path and named-volume
mount, hardening, stop-grace declaration, absence of raw replica expansion, config-check command, and settings-document
consumer sync:

```bash
python -m pytest tests/test_container_contract.py tests/test_settings.py -q
bash .codex/agent-chain/scripts/check-compose-dup-keys.sh \
  compose.yaml compose.dev.yaml compose.mock.yaml compose.prod.yaml
python - <<'PY'
from pathlib import Path
import yaml

for name in (
    "compose.yaml",
    "compose.dev.yaml",
    "compose.mock.yaml",
    "compose.prod.yaml",
):
    document = yaml.safe_load(Path(name).read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    assert isinstance(document.get("services"), dict)
PY
```

These are static checks. `command` and `healthcheck` currently run only `--check-config`; neither starts the trading
worker nor demonstrates SIGTERM queue drain. The raw Compose files omit replica expansion, but cannot prevent an
operator from passing `docker compose --scale`. Scaling the SQLite app is unsupported.

## CI contract

`.github/workflows/ci.yml` runs:

- a full-SHA-pinned checkout followed by an official Gitleaks `8.30.1` x86_64
  archive download, archive checksum verification before extraction, and executable
  checksum verification before first scanner execution;
- the Gitleaks gate before any dependency installation, repository code execution,
  JUnit, or package artifact;
- a redacted full-history scan plus generated underscore/hyphen/compact positive
  and placeholder/file-variable negative cases;
- quality checks on Python 3.11 and Python 3.14;
- pip cache keyed by `pyproject.toml`;
- critical lint;
- package-wide mypy;
- full pytest with JUnit report upload;
- sdist/wheel build;
- installed wheel import smoke;
- package artifact upload.

The workflow intentionally has no deployment job and no production secrets.
Gitleaks findings must not be copied into logs or documentation; see
[Kiwoom credential management](security/kiwoom-credential-management.md).
Deployment must be added only behind a manual approval boundary after a
concrete target environment is defined.
