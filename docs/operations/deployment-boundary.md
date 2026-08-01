# Deployment boundary

The approved target for the first container deployment is:

- EC2 instance `i-02cb0a404794bd43a` in `ap-northeast-2`;
- SSM-only administration with no inbound security-group rule;
- one public GHCR image selected by an exact OCI digest;
- one ephemeral `python -m kiwoom_stock --check-config` container.

This approval is **check-only**. It is not approval to start a worker, schedule a
process, query an account, place or revoke an order, write a production database,
or invoke Slack, S3, or Gemini.

## Four separate activation boundaries

1. **Candidate publication** builds and tests an immutable
   `sha-<full commit SHA>` image. A new tag is published once; an existing tag is
   never overwritten. The exact remote digest is inspected anonymously and
   sealed in a bounded release manifest. This workflow has no AWS, OIDC, SSM, or
   production Environment access.
2. **Production digest promotion** accepts only an approved source SHA, exact OCI
   digest, and candidate run ID. A protected workflow checks out its executor at
   the immutable workflow SHA with credentials disabled. An audit-only
   preflight rejects malformed or unapproved inputs, but produces no execution
   state. Immediately after OIDC, one Python process independently validates the
   original run, build job, unique artifact, exact source Compose bytes, and
   anonymous public image contract before it sends one bounded SSM command and
   polls it to completion. That command runs only `--check-config`.
3. **Shadow worker activation** remains RED until the process, shutdown, calendar,
   database, and side-effect boundaries below have real-path evidence.
4. **Live trading activation** is a separate, explicit approval. No workflow in
   this repository grants it.

The candidate workflow and the production promotion workflow are separate manual
`workflow_dispatch` command planes sharing one non-cancelling concurrency group.
The candidate accepts one full 40-character lowercase commit SHA and produces a
strict release manifest. It cannot contact AWS. The promotion accepts exactly
`source_sha`, `image_digest`, and `build_run_id`; it has no tag, legacy-bypass, or
arbitrary-command input. Its protected `production` Environment must contain the
same approved tuple byte-for-byte. Only after provenance, artifact/ZIP, manifest,
Compose-content, and anonymous image checks pass can it obtain OIDC. AWS permits
only the account-owned `KiwoomStock-ProductionCheck` document, not generic
`AWS-RunShellScript`, and the role cannot read Kiwoom SecureString parameters.

## Promotion trust boundary

Nothing calculated before OIDC is authoritative for execution. The promotion
workflow must not carry required hashes, sizes, artifact identifiers, parameters,
evidence, or command identifiers across OIDC through a file, step output, job
output, or `GITHUB_ENV`. The post-OIDC executor receives only the protected
immutable tuple and independently re-fetches and revalidates all derived state.
Artifact ZIP bytes, Compose bytes, SSM parameters, and the command identifier stay
in that process's memory. The only durable file is bounded redacted evidence,
written with mode `0600` by atomic replacement; the executor replaces rather than
reads the pre-OIDC audit envelope.

The pinned `aws-actions/configure-aws-credentials` v4.0.2 commit supports explicit
credential outputs but does not support disabling its environment export. The
workflow therefore maps the official outputs only into the immediately following
executor step, then an `if: always()` shell step writes empty values for all AWS
credential and region names to `GITHUB_ENV` before the evidence upload action.
No other action may appear between OIDC, execute, credential clearing, and upload.
This credential teardown is not a derived-state transport. A hard runner
cancellation between OIDC and the teardown step can prevent the clear step from
running; the credentials remain short-lived session credentials, but this
residual risk must be considered when changing the pinned action.

The executor gives the GitHub token only to bounded GitHub HTTP reads, gives no
GitHub or AWS credential to Docker, and gives only the three OIDC credentials and
fixed AWS region/retry settings to the AWS CLI. It validates the exact repository,
40-character source SHA, digest, run/job/artifact binding, modern manifest or the
single fixed legacy candidate, both immutable Compose byte hashes, runtime image
revision/entrypoint/user/850 MiB ceiling, fixed role/region/instance/document, and
the exact seven-key SSM parameter contract. GitHub reads use the pinned system
`curl` with a fixed `kiwoom-stock-promotion/1` User-Agent. The validated bearer
token is supplied only through stdin config, while argv and the minimal PATH-only
child environment contain no credential. Curl's ambient default config is disabled.
HTTPS-only initial/redirect protocol
restrictions, bounded redirects, and curl's default cross-host Authorization
stripping apply; `--location-trusted` is forbidden. Before the anonymous pull, a
cached exact digest must either be absent
with the exact daemon not-found result or be removed successfully. `send-command`
uses `AWS_MAX_ATTEMPTS=1` and occurs once; a malformed command ID is never polled.
Only exact `InvocationDoesNotExist` is retryable. Polling is limited to 90 attempts
at 10-second
intervals and succeeds only for the exact instance, response code zero, and one
exact redacted success marker.

The workflow gives every step an explicit timeout. Checkout, evidence init,
preflight, and OIDC each have one minute; execute has 18 minutes; credential clear
and evidence upload each have one minute. Their declared maximum is 24 minutes
inside the 25-minute job. The executor itself has a 960-second absolute monotonic
budget, leaving two minutes inside its step before the separately reserved two
post-execute minutes and one additional job minute. Untrusted inputs cannot extend
this local safety deadline. Every GitHub transaction, including DNS, TLS, response
headers, redirects, and slow body chunks, runs in a process group with curl
`--max-time` and a parent timeout derived from the same remaining deadline. Timeout
kills the entire process group. Docker/AWS children use the same deadline boundary.
Transport waits and command-specific timeouts never exceed the remaining budget;
binary/text stdout and stderr are bounded and raw curl errors are not evidence.

`PromotionAttemptId` is the positive decimal GitHub `run_id`. It is passed to the
allowlisted document and preinstalled root command. Under the existing nonblocking
deployment flock, the host stores a private atomic success marker bound to the
attempt ID and exact source/image/two-Compose-hash tuple. A retry of the same run
after response loss returns the same success marker without Docker work; a tuple
mismatch fails before runtime checks. Failures never create the marker. GitHub run
IDs are unique per dispatch, so a new dispatch revalidates and executes as a new
attempt rather than reusing an earlier success.

The repository document and host script changes are not applied by a merge. Before
the corrected workflow can be dispatched, the account-owned SSM document and
`/usr/local/sbin/kiwoom-production-check` must be rolled out together and read back
under a separate explicit operations confirmation. Until then the workflow and
installed host contract are version-skewed and production promotion is blocked.

Stage I contains one exact, tuple-bound compatibility path for candidate run
`30544114256`, build job `90875823290`, and its already published digest. It is
not controlled by an input or flag. After that production check succeeds, the
three approval variables must be removed or replaced and the compatibility path
must be deleted in Stage II.

## Production-check completion gate

Completion requires evidence tied to one source SHA and one image digest:

- all local quality, package, settings, and container gates pass;
- the GHCR package is public and a clean anonymous digest pull succeeds;
- the exact OIDC audience/subject and Environment protection are read back;
- IAM simulation proves the GitHub role is SSM-only;
- the host verifies instance identity, resource floors, and secret file metadata;
- the candidate receives non-secret placeholders, no network, and no production
  named volume;
- Compose renders with a required digest and one exact-name ephemeral check exits
  `0` and is absent afterward;
- current/previous full release tuples update in one atomic JSON replacement;
- cleanup is label-scoped and preserves secrets, volumes, and known-good images.

Missing package visibility, GitHub Environment approval, OIDC configuration, IAM
application, SSM execution, or host evidence is `BLOCKED`, not a presumed success.
See [GitHub-to-EC2 container deployment](github-ec2-container-deployment.md).

## Startup and shadow-worker first-activation gate

The local B1 gate proves aggregate settings validation, an explicit-date XKRX adapter, no-side-effect holiday exits,
and settings/date forwarding with fake runtime boundaries. It does not prove an actual open-session Kiwoom
authentication/runtime construction, the production host's timezone and tzdata, or how a process supervisor handles
holiday exit `0` and fatal/kill-switch exit `1`. Those C3/C4 paths remain a reachable real-path gap.

Do not turn the root `main.py` into an operational container command or enable automatic restart until an isolated
paper/shadow activation has verified the exact timezone, one open-session startup, external-client construction policy,
worker shutdown, and supervisor exit handling. B6 database/worker flush and close ownership must be complete first.
Calendar-library failure is currently treated as closed, so an exit `0` needs calendar-health evidence before operators
classify it as a normal holiday.

## SQLite and container first-activation gate

Local B6 tests prove the configured-path composition, same-file ledger/physical rows, queue drain, worker join,
idempotent close, close-before-post-market routing, and short-lived report readers with temporary SQLite files. They do
not prove the production named volume, host permissions, supervisor signals, an operational container command, or real
external report integrations.

The current common/prod Compose contract uses exactly:

- `KIWOOM_DB_PATH=/var/lib/kiwoom/trades.db`;
- `kiwoom-data:/var/lib/kiwoom`;
- non-root `10001:10001`, read-only root, `/tmp` tmpfs, and `stop_grace_period: 30s`;
- no raw `scale`/`deploy.replicas` request.

These declarations do not enforce a replica limit or graceful shutdown. `docker compose --scale` can still create an
unsupported second SQLite owner. The image command and healthcheck run only `python -m kiwoom_stock --check-config`,
which exits without starting a worker. `STOPSIGNAL SIGTERM` is present, but the application has no SIGTERM adapter that
routes to `TradingEngine.close()`. Therefore actual Docker C1/C3/C4 status remains RED.

Before enabling any worker command, obtain explicit approval and validate in an isolated non-production volume:

1. one process and one replica only, with an owner responsible for preventing CLI/supervisor scaling;
2. effective UID/GID can create, reopen, and close the exact configured DB on the named volume;
3. no cwd `trades.db` is created and schema/row/PnL/`OPEN` recovery matches characterization;
4. a real supervisor stop reaches the approved SIGTERM adapter, rejects new work, drains the queue, joins the worker,
   closes both connections, and exits within the grace period;
5. restart reads the exact `OPEN` rows without a second writer, with post-market work disabled during the stop test;
6. rollback disables the worker command and preserves the volume for read-only diagnosis.

Do not extend SQLite beyond one process/replica and local storage. A need for multiple replicas/processes, multiple
hosts, network/shared storage, sustained write contention, HA/failover, online migrations, or independent services is a
trigger for a separately planned PostgreSQL-class backend, schema migration, backup/restore, and rollback strategy.
Changing the backend or migrating operational data requires a new plan and user approval; B6 performs no migration.

## S3 archive first-activation gate

The local B2 gate uses an injected fake S3 client and temporary filesystem paths. It does not prove AWS credentials,
IAM permissions, bucket policy, object persistence, or live response fidelity. Before first activation, obtain separate
user approval and validate a throwaway object in an isolated non-production bucket and dedicated prefix. Confirm the
exact bucket, region/provider chain, upload-only IAM scope, expected object key, owner, and rollback procedure. That
validator must keep local cleanup disabled and must never use production report files. Production deployment remains
blocked while this real-path evidence is absent.

Production cleanup also requires a quiescent, single-writer output tree. Linux descriptor-relative operations pin the
configured root/date directory and immutable archive receipts bind device, inode, size, mtime, and ctime; any observed
parent or target replacement fails closed before the first deletion. POSIX does not provide an atomic
compare-identity-and-unlink operation, so the final identity check and `unlinkat`-style call still assume no
non-cooperating writer changes that entry in between. If that ownership guarantee cannot be enforced, keep cleanup
disabled and retain the archived local files instead of treating the path check as proof.
