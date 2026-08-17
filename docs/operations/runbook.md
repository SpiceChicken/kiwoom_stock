# Operations runbook

## EC2 SSH recovery and disk-full handling

The current host is `i-0e42e09d6c087ba29` at `54.116.97.199`. Human shell access
uses the repository helper and the repository-external recovery key:

```bash
./tools/ssh-direct-shell.sh
```

This is the human access path. Do not open a human Session Manager shell or use a
local `ssm send-command` as a fallback. GitHub's protected workflows still use
their exact SSM documents for automation; that separate plane must remain intact.

For a `no space left on device` or failed SSM session, connect over SSH and inspect
before deleting anything:

```bash
df -h /
df -ih /
sudo du -xhd1 /var/lib/docker /var/log /var/cache 2>/dev/null | sort -h
docker ps -a
docker image ls
docker volume ls
sudo journalctl --disk-usage
```

The approved cleanup order is:

1. Remove only exited, explicitly labelled check-only containers.
2. Remove unused Docker images after confirming the current and recorded rollback
   digests are present or otherwise intentionally retained.
3. Prune build cache only when it is not needed for an in-progress build.
4. Vacuum journald within its configured bound if necessary.
5. Re-check `df -h`, `df -ih`, Docker daemon health and the preserved volume.

Never use `docker system prune --volumes` on this host. Do not delete the shadow
named volume, host credential directory, release state or rollout backup while
recovering disk space. If the current image or rollback identity is uncertain,
stop and record the exact image/container/volume inventory instead of pruning.

After cleanup, run only the side-effect-free production check or a protected
shadow preflight. A recovered SSM Agent does not itself prove that the original
session or rollout completed.

## SSH hardening recovery

The expected host configuration is public-key-only SSH with password,
keyboard-interactive, root login and X11 forwarding disabled. Before restarting
the daemon, validate the file and keep one existing session open:

```bash
sudo sshd -t
sudo systemctl restart ssh
```

Open a second SSH connection before closing the first. For key rotation, add and
verify the new public key first, then remove the old key and update the SG admin
`/32`; never replace both access controls at once.

The current release tuple, rollout attempt, disk result and host identity are
maintained in [current-state.md](current-state.md).

## Shadow evidence validator skew

Shadow activation and stop require the rollout evidence tuple's worker,
validator, and canonical activation-document SHA-256 values. A missing
`/usr/local/libexec/kiwoom-shadow-runtime-evidence.py`, metadata other than
root:root `0750` regular/non-symlink/link-count-1, a hash mismatch, or a binding
marker mismatch is a fail-closed supply-chain event. Do not copy one file by
hand or bypass the marker. Keep activation paused, inspect the bounded rollout
audit, restore the complete attempt backup artifact set if rollback was not
confirmed, and perform a protected exact-source rollout before retrying.

The validator accepts only bounded JSON-lines or SSM invocation JSON and emits
stable safe categories; raw stdout, stderr, logs, and credentials are not copied
into failure evidence. Local validator tests are not AWS/EC2 path validation.
Unknown/missing top-level or side-effect fields, duplicate/non-finite JSON,
noncanonical KST dates, and non-finite timing evidence are schema failures and
must not be bypassed.

Do not roll out a new shadow artifact set while the fixed
`kiwoom-shadow-once` container is running or has an untrusted identity. For an
exited container only, the host may remove it under the shared lock before
install after proving the root-owned binding and installed worker/validator
hashes, exact source/image/activation labels and command, exited state, and
read-only/no-restart capability contract. Any mismatch or Docker inventory,
inspect, removal, or post-removal failure stops before backup/download/publish.
Docker Engine 28 follows Moby PR #48551 capability canonicalization: `ALL`
remains `ALL`, while every other capability is uppercased and receives the
`CAP_` prefix; normalized lists are deduplicated and sorted. Therefore the
fixed-container guard accepts `CapDrop` only as the one-item list `["ALL"]` and
accepts `CapAdd` only as either the exact legacy set
`CHOWN/SETGID/SETUID` or the exact Docker 28 canonical set
`CAP_CHOWN/CAP_SETGID/CAP_SETUID`. Order alone is immaterial. Mixed notation,
duplicates, additional capabilities, wrong types, wrong case, and empty values
remain fail-closed as `host_fixed_identity_capabilities`. The compose
declaration remains unchanged; this compatibility boundary applies only to
Docker inspect readback.
Rollout evidence records `fixed_container_recovery=removed`; an already-empty
inventory records `absent`. `preexisting_skew=true` means binding hashes
did not match observed worker/validator bytes; preserve the audit and recover the
coherent prior set rather than treating exact restoration of incoherent bytes as
success.

An exact identity rejection may record one `host_fixed_identity_*` failure
category for artifact metadata, binding shape/value, installed hashes, inspect
shape, lifecycle, config/labels, source/mode, image, activation, command, runtime
security, capabilities, or no-new-privileges. These are fixed operator-safe
categories, not Docker inspect values. Promotion requires an install invocation
with exact `Failed`/response-code `1`, exactly one `fixed-identity:` namespace
line that is allowlisted, and exactly one fixed validation-failed companion
immediately after that marker. SSM may append non-marker wrapper lines; they are
neither trusted nor persisted. Unknown or duplicate namespace markers, a missing,
duplicate, or displaced companion, oversized stderr, non-install, cancelled,
timed-out, or otherwise malformed evidence remains the generic
`host_action_failed`. Never weaken an identity guard from the category alone;
compare the expected contract with separately authorized host inspection before
making a focused correction.

If `send-command` returns an error, do not dispatch another install. The rollout
uses its exact attempt/action comment and full parameter tuple to reconcile at
most one accepted command through bounded `ListCommands` and
`ListCommandInvocations`, then waits for terminal evidence. If audit
`rollback_failure_category` is `install_acceptance_unresolved`, `host_final` is
intentionally absent and `skew=true`; keep activation paused because a late
install has not been temporally closed.

## Kill-switch terminal stop

A process exit code of `1` accompanied by `kill_switch` is an abnormal safety stop. It does **not** mean that positions
were liquidated. The engine intentionally creates no actual or paper order and leaves active position objects and ledger
rows unchanged.

When the alert or local critical log appears:

1. Do not automatically restart the same session. A supervisor restart loop could repeatedly reconnect without resolving
   the market exposure that caused the stop.
2. Record the local log timestamp, total PnL, configured loss limit, and immutable unresolved position-code snapshot.
3. Compare every unresolved code with the broker/account view and the OPEN ledger rows. Treat a missing Slack message as
   possible: the engine records whether the notifier callable returned or raised, not whether Slack delivered it.
4. Preserve local output and logs. The kill path deliberately skips the daily reporter, S3 archive, cleanup, and normal
   finish notice, so those artifacts remain available for investigation. It still attempts executor, physical-state
   adapter, queue-worker, and SQLite connection shutdown before exit.
5. Do not mark a position closed or create a liquidation order without a current price, an explicit operator approval,
   and a broker order/acceptance receipt. Those capabilities are outside the current application contract.
6. Start a new runtime only after exposure, ledger consistency, configuration, and the process supervisor's restart policy
   have been reviewed.

The current implementation and temp-SQLite tests prove the in-process queue drain/join/connection-close ordering, and the
Docker test/runtime smoke proves the disabled image and Compose lifecycle. They do not prove actual Slack delivery, real
market-hour scheduling, credentialed Kiwoom-session shutdown, named-volume permissions, or worker/DB close during a
production-like shadow container stop. Those staging checks remain activation gates; local tests must not be treated as
production readiness.

## Normal terminal outcomes

`market_closed` and an engine-caught `user_interrupt` are normal exit-code `0` outcomes. They retain the existing
post-market reporter, archive/cleanup policy, and finish-notification path. Engine persistence is closed before the
post-market report readers open the same configured SQLite path. A `KeyboardInterrupt` outside the engine or during
post-market work retains the process entrypoint's existing immediate-stop behavior.

## Persistence shutdown and recovery

The production runtime owns one `TradeLogger` at `KIWOOM_DB_PATH`. Normal and kill terminal handling follows:

1. latch the engine closed so no new cycle/evaluation starts;
2. wait for accepted evaluation work;
3. close the physical-state submission adapter;
4. append one FIFO sentinel behind accepted tasks, join the drained worker, and close worker/main SQLite connections;
5. only after successful close, enter permitted post-market work.

Repeated close is safe and does not enqueue another sentinel or close completed resources again. Cleanup attempts all
possible later phases after `BaseException`, and one bounded retry pass recovers one-shot failures before reporting the
latched failure. If sentinel publication succeeded before an interruption, a dead worker may leave duplicate control
items; these are removed only when the locked queue inspection proves every remaining item is the identical sentinel. A
physical-state task is never discarded. A persistent incomplete ledger is not marked resource-terminal and a later
explicit engine `close()` retries only that ledger step; executor and physical-adapter steps are not repeated. The main
process still makes one close attempt and enters the crash boundary rather than retrying indefinitely. An ordinary close
failure is not reported as success: normal processing enters the crash/exit-1 boundary, while a kill result remains exit
1 without a duplicate crash Slack attempt. If run and close both fail, the run error remains primary and cleanup failure
is attached as local critical context/note. Process-control exceptions remain process-control exceptions after cleanup.

If persistence shutdown fails:

1. do not start post-market readers or restart automatically;
2. preserve the configured DB, `-wal`/`-shm` companions if present, logs, and output tree without manual edits;
3. record the exact `KIWOOM_DB_PATH`, process/replica count, volume ownership/mode, primary error, and cleanup note;
4. verify there is no second application process and no unexpected cwd `trades.db`;
5. inspect `OPEN` rows read-only and reconcile them with broker/account state before any restart;
6. escalate to an isolated recovery/validator procedure. Do not delete lock/sidecar files or mark rows closed as a
   workaround.

The relative default `trades.db` exists only for legacy direct callers. Managed runs must use an explicit safe path. A
permission or missing-parent error at `/var/lib/kiwoom/trades.db` is a startup failure, not permission to create a
fallback DB elsewhere.

## Overnight-safe binary downgrade preflight

An older binary reads only `OPEN` rows, so a downgrade is blocked unless the exact preserved SQLite database has zero
`OVERNIGHT` rows. This check does not perform rollback or repair data.

1. Stop the current worker normally and verify the exact container is absent and its SQLite connections are closed.
2. Mount the preserved target volume read-only into the current r1 image or an equivalent installed r1 package.
3. Run only the following command with the absolute in-container path of that database:

   ```bash
   python -m kiwoom_stock downgrade-preflight \
     --database-path /absolute/path/to/shadow-trades.db
   ```

4. Preserve the one-line JSON evidence. Downgrade may be considered only when exit code is `0`, `status` is `PASS`,
   `active_overnight_count` is exactly `0`, `read_only` is `true`, `database_writes` is `0`, and
   `database_identity` equals the inspected mounted path.
5. Exit code `2`/`BLOCKED` means one or more OVERNIGHT rows remain. Exit code `1`/`FAILED`, stale evidence, relative or
   different paths, and the unrelated ephemeral `--rollback-check` are not downgrade approval.

The command never opens the source with SQLite. On Linux it reads source main/WAL/SHM/journal inventory through raw
`O_NOFOLLOW | O_NOATIME` read-only handles, copies only main and WAL into an isolated temporary directory, and opens
SQLite only on that temporary copy. Source SHM is attested but not copied. Any source rollback journal blocks approval
with `SOURCE_BUSY`; a source-family change during inspection fails with `SOURCE_CHANGED`. If no-atime access cannot be
guaranteed, `NOATIME_UNAVAILABLE` fails closed rather than retrying without that protection. Run the command as the
database owner (or with the narrowly administered filesystem capability required for `O_NOATIME`) while the worker is
stopped. A blocked database must remain on the current binary until normal next-session reconciliation, or until a
separately approved backup and administrative data-reconciliation procedure exists.
