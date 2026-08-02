# Container development

This repository separates container intent into four files:

- `compose.yaml`: common service contract and side-effect-safe default command;
- `compose.dev.yaml`: local/offline development and test target;
- `compose.mock.yaml`: staging-only mock endpoint credential wiring;
- `compose.prod.yaml`: production-like validation template with secrets and hardened runtime settings.

No compose file starts live trading by default. The runtime command is currently:

```bash
python -m kiwoom_stock --check-config
```

That command validates settings and exits without constructing Kiwoom, Slack, S3, Gemini, or order adapters.
The healthcheck runs the same config-only command. Consequently the current image/Compose contract is a build and
configuration-validation boundary, not an enabled trading worker.

## SQLite volume and process ownership

The common Compose file pins:

```text
KIWOOM_DB_PATH=/var/lib/kiwoom/trades.db
kiwoom-data:/var/lib/kiwoom
```

The application root filesystem remains read-only, `/tmp` is tmpfs, and only the named data volume is intended for
SQLite and report output. The runtime image uses UID/GID `10001:10001`; before activation, verify the mounted directory
is writable by that identity and that both ledger and report readers resolve the exact configured path. Never fall back
to a cwd `trades.db` after a permission failure.

This SQLite topology supports exactly one application process, one replica, and one local-volume writer owner. The raw
common and production override files do not request `scale` or `deploy.replicas`, but Compose does not enforce this
limit: an operator can still pass `--scale`. Do not do so. Multiple application processes/replicas, multi-host or network
storage, sustained locking, HA, or online migration needs trigger a planned PostgreSQL-class backend migration.

## Development compose

```bash
docker compose -f compose.yaml -f compose.dev.yaml config
docker compose -f compose.yaml -f compose.dev.yaml run --rm app
```

The dev override:

- builds the `test` Docker target;
- bind-mounts the source tree;
- uses fake Kiwoom settings;
- runs tests;
- uses `network_mode: none` for the running service.

## Production-like compose

```bash
KIWOOM_PROD_APP_KEY_FILE=/secure/kiwoom/prod/KIWOOM_APP_KEY \
KIWOOM_PROD_SECRET_KEY_FILE=/secure/kiwoom/prod/KIWOOM_SECRET_KEY \
docker compose -f compose.yaml -f compose.prod.yaml config
```

The production-like template:

- uses an image, not a source bind;
- runs as non-root UID/GID `10001:10001`;
- sets `read_only: true`;
- drops Linux capabilities;
- enables `no-new-privileges`;
- mounts data under `/var/lib/kiwoom`;
- reads exact secret targets through `KIWOOM_CREDENTIALS_DIR=/run/secrets`.

It inherits the common configured DB path, named volume, `/tmp` tmpfs, and `stop_grace_period: 30s`. That grace value and
the image `STOPSIGNAL SIGTERM` are declarations only. The application has not yet wired SIGTERM to the typed session
result and engine close path, and the active command exits after config validation. No graceful worker-stop claim can be
made from these files alone.

Mock wiring is separate and is valid only with `KIWOOM_APP_ENV=staging`:

```bash
KIWOOM_MOCK_APP_KEY_FILE=/secure/kiwoom/mock/KIWOOM_APP_KEY \
KIWOOM_MOCK_SECRET_KEY_FILE=/secure/kiwoom/mock/KIWOOM_SECRET_KEY \
docker compose -f compose.yaml -f compose.mock.yaml config
```

All four host variables above must resolve to absolute files outside the
repository. Compose interpolation can require a value but cannot prove that it
is absolute or repository-external, and the provider inside the container
cannot prove where a mounted file lived on the host. A launcher/host validator
must enforce that boundary before activation. Until such a validator and an
actual UID/GID/mode/readability smoke exist, mock/prod activation is BLOCKED and
must remain DISARMED.

## Bounded shadow-once compose

`compose.shadow.yaml` is a separate, restart-disabled contract for the fixed one-cycle shadow worker. It uses the
`kiwoom-shadow-data` named volume, `/var/lib/kiwoom/shadow-trades.db`, a read-only root filesystem, 30-second stop
grace, and external secret files. Compose starts the image as root only for `runtime_entrypoint.py` to copy the two
file secrets into `/run/kiwoom-secrets` and drop to runtime UID/GID `10001:10001`; the application itself is never
run with root privileges. The source SHA, image digest, and activation ID are required non-secret tuple inputs. It
is a contract/preflight file only until a separately approved validator proves the mounted volume permissions and
real market-read path; it must not be scaled or started with the production-check override.

The worker translates SIGTERM/SIGINT into a cooperative stop event. Checkpoints exist before and after credential,
HTTP, snapshot, calculation, and local persistence boundaries, with a monotonic 30-second shutdown budget. An
already in-flight HTTP request can still consume the transport's remaining, clamped timeout (the configured market
upper bound is `(5, 30)` seconds); the post-response checkpoint then fails closed and the normal resource owner
performs cleanup.

Do not submit credential values through repository files, `.env`, Compose
arguments, chat, or CI. The complete delivery and rotation contract is in
[Kiwoom credential management](security/kiwoom-credential-management.md) and
[credential rotation](operations/credential-rotation.md).

Do not rotate either mounted file in place. Stop the worker, prepare both files
in a new hardened external directory, update both host file references
together, and restart so a new provider instance captures one directory
generation. The provider rejects directory/file replacement or pair mutation
during a load; a detected race fails startup instead of returning mixed
generations.

## Current validation status

A Docker executable path may exist on the host, but this work did not invoke the Docker CLI or daemon under the approved
no-external/operational-side-effect boundary. Docker build, Compose render, and runtime smoke are therefore not marked
PASS. Static tests verify the intended file contract until an explicitly approved Docker-enabled verifier can run:

```bash
docker build --target test -t kiwoom-stock:test .
docker build --target runtime -t kiwoom-stock:runtime .
docker compose -f compose.yaml -f compose.dev.yaml config --quiet
docker compose -f compose.yaml -f compose.mock.yaml config --quiet
docker compose -f compose.yaml -f compose.prod.yaml config --quiet
```

After an operational worker command and SIGTERM adapter are separately approved, an isolated Docker validator must also
prove: effective UID/GID write access, one DB file at the configured volume path, no cwd fallback DB, accepted queue
drain, worker exit, both connections closed, stop completion within the grace period, and restart recovery of exact
`OPEN` rows. Until that evidence exists, Docker/C1/C3/C4 real-path status remains RED.
