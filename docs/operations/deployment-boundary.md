# Deployment boundary

The current automation target for the first container deployment is:

- one EC2 instance in `ap-northeast-2`; exact instance identity is maintained in
  AWS/private operator inventory, not this public document;
- human administration through restricted SSH (TCP 22 from the current operator
  `/32` only); GitHub automation remains on the exact SSM command plane;
- one public GHCR image selected by an exact OCI digest;
- one ephemeral `python -m kiwoom_stock --check-config` container.

This approval is **check-only**. It is not approval to start a worker, schedule a
process, query an account, place or revoke an order, write a production database,
or invoke Slack, S3, or Gemini.

## Human access versus automation access

The access planes are intentionally separate:

- Human operators use [`tools/ssh-direct-shell.sh`](../../tools/ssh-direct-shell.sh)
  with the repository-external `ubuntu` SSH key. The host SSH daemon is public-key
  only, and the security group admits only the current administrator's `/32`.
- The local AWS role is used for AWS identity, inventory, health and read-back. It
  is not a substitute for the SSH shell and operators must not use
  `aws ssm start-session` for routine host access.
- Protected GitHub workflows still use account-owned SSM documents for
  production-check, shadow rollout and shadow activation. Their exact document,
  instance, parameter and timeout boundaries remain unchanged. Switching human
  access to SSH did not migrate or remove this CI backend.
- SSM Agent therefore remains active on the host. “SSH management” means the
  human path, not that SSM is disabled globally.

The current host's logical status, disk-recovery result, SSH hardening and release
tuple are recorded in [current-state.md](current-state.md). Exact host/network
identifiers remain in AWS/private operator inventory.

## Five separate activation boundaries

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
3. **Shadow worker rollout** accepts one exact main source SHA and uses a
   separate protected OIDC role to install/read back the immutable host worker,
   standalone evidence validator, and activation-document artifact set. It does
   not start or stop a container.
4. **Shadow worker activation** is a separate protected, bounded command plane.
   It admits exact `oneshot`, `continuous`, or `stop` actions and never grants
   order or account capability.
5. **Live trading activation** is a separate, explicit approval. No workflow in
   this repository grants it.

The candidate workflow and the production promotion workflow are separate manual
`workflow_dispatch` command planes sharing one non-cancelling concurrency group.
The candidate accepts one full 40-character lowercase commit SHA and produces a
strict release manifest. It cannot contact AWS. The promotion accepts exactly
`source_sha`, `image_digest`, and `build_run_id`; it has no tag, legacy-bypass, or
arbitrary-command input. Its protected `production` Environment must contain the
same approved tuple byte-for-byte. Only after provenance, artifact/ZIP, manifest,
Compose-content, and anonymous image checks pass can it send SSM. OIDC occurs
after the fixed tuple/audit preflight and supplies outputs to the authoritative
executor; those derived-state checks therefore run after OIDC but before SSM.
AWS permits only the account-owned `KiwoomStock-ProductionCheck` document, not
generic `AWS-RunShellScript`, and the role cannot read Kiwoom SecureString
parameters.

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

The operational order is fixed: trusted executor checkout → fixed tuple/audit
preflight → Node 24 OIDC outputs → authoritative run/job/artifact/Compose/image
validation → one exact SSM command → credential clear → evidence upload. After
terminal success/failure/cancel, operators delete all three temporary
approval tuple variables and read back the Environment as role-only, secrets `0`,
and pending deployments `0`. A later release registers a fresh tuple; it does not
retain or replace a prior terminal run's tuple in place.

The pinned `aws-actions/configure-aws-credentials` v6.2.3 Node 24 commit disables
environment credential export and emits explicit credential outputs. The workflow
maps those outputs only into the immediately following executor step, then an
`if: always()` shell step writes empty values for all AWS credential and region
names to `GITHUB_ENV` before the evidence upload action.
No other action may appear between OIDC, execute, credential clearing, and upload.
This credential teardown is not a derived-state transport. A hard runner
cancellation between OIDC and the teardown step can prevent the clear step from
running; the credentials remain short-lived session credentials, but this
residual risk must be considered when changing the pinned action.

The executor gives the GitHub token only to bounded GitHub HTTP reads, gives no
GitHub or AWS credential to Docker, and gives only the three OIDC credentials and
fixed AWS region/retry settings to the AWS CLI. It validates the exact repository,
40-character source SHA, digest, successful run/job/unique release-manifest
binding, both immutable Compose byte hashes, runtime image
revision/entrypoint/user/850 MiB ceiling, fixed role/region/instance/document, and
the exact seven-key SSM parameter contract. GitHub reads use runner-provided
`curl` with a fixed invocation/policy and `kiwoom-stock-promotion/1` User-Agent;
the binary path and version are not pinned. The validated bearer
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

The account-owned SSM document and `/usr/local/sbin/kiwoom-production-check` were
rolled out and read back together during Stage I. Stage II does not change that
manifest-agnostic execution plane; any future repository change to either file
again requires coordinated rollout and read-back before promotion.

Stage I's exact tuple-bound compatibility path completed its one production check
and was removed in Stage II. Every new promotion now requires a completed,
successful candidate run and one strict `release-manifest.json`; the retired
cancelled run, `candidate-<source>` artifact, and two-member ZIP are fail-closed.

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

## Semantic downgrade boundary

Additive SQLite columns do not make an old binary semantically safe while active `OVERNIGHT` rows exist. Before any
separately approved old-binary activation, the stopped current r1 binary must inspect the exact preserved mounted DB via
`python -m kiwoom_stock downgrade-preflight --database-path ABSOLUTE_DB`. Only exit `0` evidence with schema `1`, exact
DB identity, `PASS`, count `0`, `read_only=true`, and `database_writes=0` can satisfy this precondition. `BLOCKED` or
`FAILED` evidence causes no status conversion and forbids activation.

This repository change provides the read-only command seam only. It does not wire or authorize host rollback,
deployment, old-image activation, automatic data correction, or an arbitrary command surface. Those remain a separate
approval and C4 real-path validation boundary.

## Bounded shadow activation boundary

For the market-hours schedule and tuple registration, see
[shadow session scheduling](shadow-session-scheduling.md).

The shadow activation artifacts are deliberately separate from the check-only
promotion path:

- `.github/workflows/cd-shadow-worker-activation.yml` accepts only an exact
  source SHA, public GHCR digest, and bounded activation ID. `oneshot` and
  `continuous` additionally require a successful candidate run and shadow
  Compose hash. Before OIDC, every state including `stop` uses the authenticated
  GitHub compare API to prove the source is the current protected-main workflow
  SHA or its ancestor. This preserves an old deployed-main stop while rejecting
  arbitrary branch commits before checkout Python is imported or executed;
- `KiwoomStock-ShadowWorker` accepts only `oneshot`, `continuous`, or `stop` and invokes the
  root-owned `/usr/local/sbin/kiwoom-shadow-worker` on the fixed EC2 instance;
- `deploy/ec2/shadow_worker_control.sh` verifies instance identity, root-owned
  `0400` credential files, image revision/user/entrypoint, and the exact
  `compose.shadow.yaml` hash before running one exact container;
- `/usr/local/libexec/kiwoom-shadow-runtime-evidence.py` is the sole schema 2/3
  evidence validator. Both the host adapter and checked-out workflow execute
  those exact stdlib-only bytes; neither reconstructs evidence predicates. It
  rejects duplicate/non-finite JSON, noncanonical dates, unknown/missing
  event-specific fields, and unknown/missing side-effect keys, and emits only a
  canonical projection of validated safe fields;
- Docker pull and Compose orchestration progress is redirected away from SSM
  stdout. That channel carries only the host's canonical validator projection
  and fixed non-JSON status markers, so untrusted progress such as `[+]` cannot
  be mistaken for a truncated or malformed evidence record by the workflow's
  second validation pass;
- the shadow named volume is never removed by the executor, and the command
  reports only redacted tuple/status evidence.

One-shot remains the Compose default. Continuous render is selected only by the
root-owned host executor with the exact mode/process/CLI triple; arbitrary shell
or command input is not accepted by workflow or SSM. Continuous start is detached
only after a first redacted safe cycle is observed. That versioned evidence must
report exactly six HTTP attempts, one call to each allowlisted market endpoint,
and strict integer local counters. Status is exactly one, error/critical are zero,
and one cycle permits no paper transition, one buy, or one sell; missing, extra,
boolean, floating-point, out-of-range, or simultaneous buy/sell values fail
activation. It uses a fresh one-shot
runtime per cycle, a 60-second completion-to-start gate, one process lock, a
seven-hour outer cap plus an absolute 15:30 KST session close, `restart: "no"`,
and 30-second signal shutdown. Stop targets
only the exact container. The expected source SHA, image digest, and activation
ID travel through workflow, SSM, host arguments, container labels/config, and
terminal JSON. Mismatch or container absence is a nonzero failure. After exact
identity comparison, stop verifies either a clean signal transition
(`STOPPED`/`stop-requested`) or an already-exited natural cap
(`DEADLINE`/`run-deadline`), requires a non-137 zero exit, removes that exact
container, and preserves its named volume and image.

Continuous evidence uses schema version `3` and records the elapsed start time
of each cycle, the observed interval between cycle starts, whether the current
cycle reopened the same database identity, and the cumulative reopen count.
The first safe tick must have no prior interval and `db_reopened=false`; a
terminal stop/deadline record must bind `db_reopens` to `cycles - 1` and, once
two cycles exist, prove an observed interval of at least 60 seconds whose value
matches the difference between the first two cycle start timestamps. A database
identity change between cycles fails closed. This makes the bounded start/stop
artifact carry direct evidence of the fresh-runtime and same-isolated-ledger
contract rather than only the configured interval.

Activation also requires exact worker and validator SHA-256 values plus the
deterministic canonical activation-document SHA-256 recorded by rollout evidence.
The root-owned worker compares its own root:root `0750` bytes, the validator's
fixed root:root `0750` regular/non-symlink/single-link installation, and the
root-only `0600` binding marker before any `oneshot`, `continuous`, or `stop`
logic. A
missing marker, mismatch, incomplete rollback, or document/host skew fails
before Docker or Kiwoom execution.

The activation role has read-only `DescribeDocument`/`GetDocument` on that exact
activation document. Immediately before `SendCommand`, the workflow reads the
numeric `DefaultVersion`, requires `Status=Active`, canonicalizes that version's
JSON content with duplicate-key rejection, and compares its hash with rollout
evidence. It sends that explicit numeric version. `$LATEST` is forbidden;
therefore a failed rollout that leaves a newer non-default version cannot bypass
default rollback.
The activation artifact records that attested numeric version and all three
strict artifact-set hashes beside the command ID/status. Rollout host
before/new/reconciled/final evidence records bounded owner, mode, link count,
regular-file and metadata-valid fields for worker, validator, and binding; raw
stdout and credentials remain excluded.

The protected `production-shadow` Environment must provide the distinct
`KIWOOM_AWS_SHADOW_ROLE_ARN` variable. The role policy is limited to the custom
SSM document, the fixed instance, and `ssm:GetCommandInvocation`; it does not
read Kiwoom SecureString parameters. Registering that document, role, host
script, and Environment is an external change and must be read back before the
first activation.

## Protected rollout-document migration boundary

Rollout-document writes are isolated in
`.github/workflows/cd-shadow-rollout-document-migration.yml`. The job is
main-only, uses `production-shadow`, exact source checkout/clean provenance,
and the same non-cancelling concurrency group as rollout and activation. It
assumes only `KIWOOM_AWS_SHADOW_MIGRATION_ROLE_ARN`; neither routine rollout nor
activation role receives rollout-document migration or Parameter Store state
authority.

The migration role can read/update/default only
`KiwoomStock-ShadowWorkerRollout` and can access only its fixed lease plus
attempt-journal Parameter Store paths. It has no Create/DeleteDocument,
SendCommand, EC2, Kiwoom credential, account, order, cancel, or revoke authority.
The lease is create-only and has no stale takeover. Each attempt journal is
bounded to 4KiB. Its immutable equality contract binds the stable account,
exact IAM role fingerprint, source/attempt, approved prior version/hash, target
hash, immutable VersionName, and executable/blob provenance. GitHub run/session
names and assumed-role session fingerprints are deliberately excluded so a new
protected run can reconcile the same attempt. Every run still exactly attests
its STS account, assumed role, and expected session; only a redacted actor
observation is updated in the journal and local artifact. Document bodies,
credentials, raw ARN, and AWS stderr are never stored.

Attempt ownership is journal-first. `apply` create-only writes an
`attempt_created` journal before touching the lease. It then acquires/read-backs
the fixed lease and durably advances to `lease_acquired`. `reconcile` first opens
the existing journal read-only and verifies the stable contract, then acquires
the lease and re-reads the journal to detect races. An existing apply journal or
contract mismatch therefore creates no lock; a crash after journal creation but
before lease acquisition can be resumed by the same attempt.

Every update and cutover has a durable remote prewrite phase and a submit budget
of one. Process or response loss is reconciled from authoritative
SSM state and the same VersionName; uncertain state is never retried. Because
UpdateDocumentDefaultVersion has no CAS, a prior default still observed after a
submitting phase becomes manual-hold rather than inferred failure/success.
Manual-hold retains the lease until a separately approved incident recovery.
`complete` and `failed_safe` are explicit terminal phases that
may release only an exact-owner lease; only `complete` exits zero. Release-only
reconcile does not trust terminal journal status alone: complete re-proves the
exact target and migrated VersionName, failed-safe proves the same-attempt
candidate is absent on both immediate terminalization and later reconcile.
SSM document versions are immutable, so this migration has no automatic rollback
phase or prior-default write authority. After forward cutover, exact target state
becomes complete; any third latest/default, name/status/content/ownership drift,
malformed terminal
evidence, or transiently unreadable state cannot produce PASS or release;
authoritative drift becomes manual-hold and transient reads retain the phase. A global
monotonic deadline starts before Git provenance. Its primary cutoff forbids
ordinary progress, UpdateDocument, and cutover while a reserved terminal budget
remains for durable manual-hold, terminal reconciliation/journal, and exact-owner lock
release. Pagination, settling, AWS calls, and Git subprocesses all consume that
same absolute budget.

The approved Git blob is passed directly as `--content`, eliminating mutable
`file://` rereads. Protected artifacts are redacted local summaries; the remote
journal is recovery SSOT. Real IAM condition behavior, SSM visibility,
cross-run lease ownership, and default transition remain C2/C4 validator work
and are not proved by local mocks or the static checker.

## Protected shadow rollout boundary

`.github/workflows/cd-shadow-worker-rollout.yml` has one required, no-default
`source_sha` input. It requires `refs/heads/main`, equality with the trigger SHA,
and exact-SHA checkout before OIDC. Region, instance, document names, raw GitHub
URL, host paths, and actions are fixed. The separate
`KIWOOM_AWS_SHADOW_ROLLOUT_ROLE_ARN` can run only the fixed rollout document on
the exact instance and update only the exact activation document; activation
role gains only the exact-document read-only attestation actions described above.

The rollout role additionally has read-only `DescribeDocument`/`GetDocument` on
the exact rollout document. Before its first host command, every routine run
requires rollout document `Status=Active`, numeric default/latest equality at
any vN, exact semantic structure, and the deterministic canonical content hash
derived from the checked-out source. The attestation returns that exact version
and hash. Every `SendCommand`, response-loss acceptance-history match, and node
invocation is bound to the same version. Immediately before each send the
executor describes the document again; default/latest drift fails closed before
a host command. The normal `SendCommand` response must return the same document,
version, fixed instance, comment, and submitted parameter tuple. Every polled
invocation must return the same command ID/document/version/instance, and its
terminal host evidence binds the complete rollout tuple. Missing attestation
fields and placeholder hashes are rejected. Bootstrap attestation alone is not
sufficient.

Only when the previous activation default differs from the checked-out
pre-exec-lock document, the executor performs a one-time legacy transition
drain before its first host command. It explicitly pages metadata-only
`ListCommands` acceptance/aggregate history filtered by exact instance plus
`KiwoomStock-ShadowWorker`, then cross-checks node execution state through
metadata-only `ListCommandInvocations`. Both use explicit bounded service
pagination. Every aggregate command and node invocation must be terminal, and
none may have been requested during the preceding 3,600 seconds. Requiring the
aggregate plane prevents an accepted Pending command that has not yet produced
a node invocation from escaping the drain. A single snapshot is insufficient:
the complete acceptance/execution scan pair runs three times at monotonic
offsets 0, 30, and 60 seconds. Every scan must pass, and the final execution
scan immediately precedes the first rollout host command while shared
non-cancelling concurrency remains held. The one-hour quiet window
conservatively exceeds the activation document's 1,020-second delivery/execution
budget and leaves margin for delayed SSM visibility. Malformed responses,
timestamps/statuses, pagination ambiguity, any nonterminal command, or any
recent command fail before host mutation. Once the new document is already the
default this is steady mode and the gate is `n-a`. Audit records mode, checked
timestamp, quiet-window size, required/completed scan counts, configured and
observed settling seconds, first/last checked timestamps, and bounded per-scan
aggregate-command/node-invocation total/recent/nonterminal counts and results.
The shared non-cancelling concurrency group prevents a new activation from
starting after this gate while the rollout remains in progress.

The executor recalculates worker and validator raw hashes plus activation-document
raw/canonical and rollout-document hashes. It captures pre-state, installs and
reads back the host artifact set, creates and defaults one activation-document version, requires semantic
and canonical-byte read-back, then reads the host again. Failure restores and
confirms the previous document default first, then restores the exact attempt
backup. Uncertain rollback records `skew=true`; activation stays paused. Audit is
bounded/redacted, atomic mode `0600`, retained 14 days, and excludes credentials,
source bodies, and raw command output.

The host transaction uses the same fixed exclusive flock as activation. Worker,
validator, and binding are each completed in private temporary files on their
destination filesystem, checked for owner/mode/hash (plus worker `bash -n` and
validator Python compilation), file-fsynced, atomically renamed, then
parent-fsynced. Validator publishes before worker and binding is the commit point.
Rollback uses the same
primitive and an exact attempt manifest. The executor marks install as applying
before submission; terminal/evidence/transport ambiguity triggers bounded host
read-back. A lost `send-command` response is never retried: after the immediate
read-back, the executor uses the existing read-only command history permissions
to identify one command by exact rollout attempt/action comment, rollout
document/version, instance, and complete parameter tuple, cross-checks one node
invocation, and waits for its terminal evidence. A reconciled successful install
may continue; a reconciled terminal failure or evidence mismatch enters the
normal exact rollback. Missing, multiple, malformed, or tuple-mismatched history
remains `skew=true`, and the audit deliberately leaves `host_final` unset because
a late command cannot be proven absent. Audit records per-action command acceptance, ID,
terminal status/response, host before/new/final, default reconciliation, and
separate rollback failure category.
History pages validate every command's positive document-version syntax, ignore
unrelated commands from older valid versions, and reject an otherwise exact
comment/parameter tuple if it names a different rollout-document version.

Before any install backup, exact-SHA download, or publish, the host checks under
that same lock whether the exact fixed name `kiwoom-shadow-once` exists. Running,
dead, created, ambiguous, or mismatched identity states reject rollout. An exited
container is removable only after a root-owned single-link binding and the
installed worker/validator bytes agree, and its source/image/activation labels,
command, user, read-only root, no-restart, capability, and no-new-privileges
settings all match the bounded continuous contract. The exact name is removed,
absence is read back, and host evidence reports `fixed_container_recovery` as
`removed`; an initially empty inventory reports `absent`. Docker daemon,
permission, inventory, inspect, validation, removal, or post-removal failure is
fail-closed. Prestate is
classified as all-absent, coherent legacy
worker/binding, coherent current artifact set, or incoherent. Incoherent
binding-to-observed hashes set `preexisting_skew=true`/`skew=true` and stop before
install rather than later claiming a healthy rollback.

The stable activation SSM document acquires that lock before it opens or execs
the mutable worker path and passes inherited FD `9`. The worker verifies the FD
is open, resolves to the exact approved lock inode, and can exclusively reuse
the lock before any self-hash/binding guard. A direct host invocation receives
no inherited FD and acquires the same lock itself. An argv/environment marker
without the real approved lock FD cannot bypass this check.

`deploy/check_shadow_ssm_contract.py` is the project-authoritative SSM D2 gate.
It duplicate-safely parses both workflows and documents, then connects the
activation workflow to the installed worker's actual option parser and the
rollout workflow to the Python executor. The activation AWS CLI allowlist is
exactly one host `send-command`, one polling `get-command-invocation`, and one
final evidence `get-command-invocation`. The rollout executor write allowlist is
exactly three sites: one host `send-command`, one activation-document update,
and one default-version update. At runtime, every `AwsCli.call` tuple is also
classified against the complete current read/write command allowlist before a
subprocess starts; a missing or incorrect caller `write` classification fails
closed. The checker verifies every call site, the sole AWS subprocess seam,
the rollout module's complete subprocess import/attribute/binding surface, and
all supported shell write primitives for protected inputs at quote/comment-aware
command-unit boundaries. It also fixes the worker mode guard position, requires
the CI checker as the sole non-comment executable command in its step, and
normalizes supported `command`, `env`, and absolute Python build launchers before
checking the CI build/package dependency DAG.
Exact input/environment/target/flag mappings, document schemas,
terminal/evidence handling, and artifact installation wiring remain required.
CI runs this checker explicitly; the generic agent-chain checker's
unsupported exit `2` is not treated as PASS. This static gate does not prove
AWS eventual consistency or the EC2/Docker real path, which remains a separate
validator boundary.

Attempt backup publication is also atomic and durable. Prior worker/validator/binding or
their absence sentinels plus the manifest are created in a private staging
directory, checked for root ownership/mode/link/hash, individually fsynced, then
the directory is fsynced and atomically renamed to the final attempt directory;
the state parent is fsynced afterward. A sealed exact same-attempt backup is
reusable after host-side rollback, while an incomplete private staging directory
does not occupy the attempt ID and a different tuple fails closed.

Rollout and activation share concurrency group
`kiwoom-stock-shadow-i-0e42e09d6c087ba29` with cancellation disabled. Rollout
success is not activation approval. Until validator evidence proves real AWS/EC2
bootstrap, install/read-back, negative IAM decisions, and rollback, the external
path remains unverified.

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
idempotent close, close-before-post-market routing, and short-lived report readers with temporary SQLite files. Docker
test/runtime images and the disabled dev Compose build/start/exit path have also been executed. These checks do not
prove the production named volume, credentialed shadow worker, external report integrations, or real staging API path.

The current common/prod Compose contract uses exactly:

- `KIWOOM_DB_PATH=/var/lib/kiwoom/trades.db`;
- `kiwoom-data:/var/lib/kiwoom`;
- non-root `10001:10001`, read-only root, `/tmp` tmpfs, and `stop_grace_period: 30s`;
- no raw `scale`/`deploy.replicas` request.

These declarations do not enforce a replica limit or graceful shutdown. `docker compose --scale` can still create an
unsupported second SQLite owner. The common image command and healthcheck run only `python -m kiwoom_stock --check-config`,
which exits without starting a worker. The bounded shadow worker has a `ShadowStopController` SIGTERM/SIGINT adapter
that feeds the shutdown budget and runtime close path, but a credentialed production-like container stop has not yet
been executed. Docker build/disabled Compose checks are PASS; staging C1/C3/C4 evidence remains open.

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
