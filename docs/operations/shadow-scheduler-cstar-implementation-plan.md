# C* Shadow schedule implementation plan

## Status

Implemented and repaired — 2026-09-02. First post-repair market-day acceptance
evidence is preserved; the EventBridge SSM invocation-event pattern was corrected
and deployed. Automatic delivery on the next real SSM invocation remains the final
operational acceptance gate.

이 문서는 구현 순서와 write set 및 실제 cutover read-back을 기록한다. AWS apply,
EventBridge enable, GitHub schedule 제거, SSM/EC2 실행은 P8에서 완료되었고 첫
개장일의 end-to-end evidence만 후속 acceptance 항목이다. 2026-08-24 보수에서
submitter rejection audit, fail-closed ledger bootstrap, explicit schedule state,
그리고 Lambda immutable-version refresh를 추가하고 새 production-check/
exact-rollout tuple로 read-back했다.

## Root reconciliation decisions

planner 초안의 다음 두 부분은 이 canonical 계획에서 교정한다.

1. `release_id`는 Scheduler input 또는 schedule-generation metadata에 넣지 않는다.
   application release와 schedule protocol generation을 분리한다. start submitter가
   `ACTIVE` release lease를 읽어 daily session lease에 동결하고, stop은 그 session lease만
   사용한다.
2. 현재 incumbent를 보호하기 위해 기존 `KiwoomStock-ShadowWorker` document를 C* protocol로
   직접 변경하지 않는다. C* activation과 evidence export는 별도 exact documents로 만든다.

DynamoDB TTL이 설정된 item에서 `expires_at=0`은 즉시 만료 대상으로 해석될 수 있으므로
사용하지 않는다. 보존 중인 item은 TTL attribute 자체를 생략하고 terminal closure 뒤에만
positive epoch-seconds를 기록한다.

## Target modules and contracts

### Pure cloud contract

신규 `deploy/shadow_cstar_contract.py`가 다음 순수 계약을 소유한다.

- strict JSON key sets and scalar bounds;
- Scheduler context projection;
- KST session/activation/occurrence derivation;
- canonical compact JSON and SHA-256 identity;
- cloud ledger state transitions;
- immutable release and session lease values;
- safe diagnostic/metric category allowlists.

Scheduler payload의 exact fields:

```json
{
  "schema_version": 1,
  "phase": "start|stop",
  "schedule_generation": "cstar-g000001",
  "schedule_arn": "<aws.scheduler.schedule-arn>",
  "scheduled_time": "<aws.scheduler.scheduled-time>",
  "execution_id": "<aws.scheduler.execution-id>",
  "attempt_number": "<aws.scheduler.attempt-number>"
}
```

`execution_id`와 `attempt_number`는 delivery attempt evidence다. occurrence identity에는
포함하지 않는다.

```text
session_date_kst = KST date(scheduled_time)
activation_id   = shadow-session-YYYYMMDD
occurrence_id   = sha256(canonical_json(
  schema_version, schedule_generation, schedule_arn,
  scheduled_time, phase, session_date_kst
))
```

start scheduled time은 exact weekday `08:50 KST`, stop은 `15:35 KST`여야 한다.

### Cloud ledger

신규 C* 전용 DynamoDB table은 existing GitHub missing-run detector table과 분리한다.

| item | PK/SK | 역할 | terminal retention |
|---|---|---|---|
| release intent | `RELEASE#<release_id>/META` | exact immutable artifact tuple | no TTL while referenced |
| active release pointer | `CONTROL#CSTAR/RELEASE` | start가 선택할 ACTIVE release | no TTL |
| schedule generation | `GEN#<generation>/META` | exact pair ARN/protocol hashes | no TTL |
| daily session lease | `SESSION#YYYY-MM-DD/LEASE` | start/stop same release/activation | 400 days after CLOSED |
| occurrence | `OCC#<occurrence_id>/META` | independent submission/command/runtime/closure states | 400 days after closure |
| command attempt | `OCC#<occurrence_id>/CMD#<command-id>` | all known SSM attempts | 400 days |

release ID는 exact release intent canonical bytes의 SHA-256이다. schedule generation row에는
release ID를 넣지 않는다.

Occurrence state dimensions:

```text
submission: CLAIMED -> SUBMITTING -> SUBMITTED | AMBIGUOUS | REJECTED
command:    UNKNOWN -> PENDING -> IN_PROGRESS -> SUCCESS | FAILED | TIMED_OUT |
            CANCELLED | UNDELIVERABLE | TERMINATED
runtime:    UNKNOWN -> ACCEPTED | CLOSED_HOLIDAY | STOPPED | DUPLICATE |
            STALE_GENERATION | FAILED | AMBIGUOUS
closure:    OPEN -> EVIDENCE_PENDING -> CLOSED | ALERTED
```

각 차원은 conditional version update로만 전진한다. terminal/closed item은 duplicate 또는
out-of-order event로 reopen하지 않는다.

start submitter transaction:

1. exact active schedule generation과 schedule ARN 확인;
2. active release pointer와 immutable release intent 확인;
3. daily session lease를 absent 조건으로 생성하거나 exact existing lease 재사용;
4. occurrence/attempt claim;
5. transaction commit 뒤 exact SSM command submit;
6. command ID를 별도 conditional update로 기록.

stop submitter는 ACTIVE release pointer를 읽지 않는다. exact daily session lease가 없으면
`REJECTED_NO_SESSION`으로 닫고 SSM을 호출하지 않는다.

### Host durable fence

신규 standalone `deploy/ec2/shadow_schedule_fence.py`를 rollout으로 root-owned
`/usr/local/libexec/kiwoom-shadow-schedule-fence.py`에 설치한다. authoritative state는
`/var/lib/kiwoom-stock/shadow-schedule/fence.json`, parent `0700`, file `0600`,
`root:root`, regular one-link/no-symlink다.

Authority:

```json
{
  "schema_version": 1,
  "authority": {
    "clock_owner": "eventbridge-scheduler",
    "active_schedule_generation": "cstar-g000001",
    "protocol_sha256": "<64hex>",
    "armed_at": "<RFC3339Z>"
  },
  "sessions": {},
  "occurrences": {}
}
```

Session record pins session date, activation ID and release ID. Occurrence record pins schedule
generation/ARN/time, phase, release ID and transitions:

```text
CLAIMED -> APPLYING -> EFFECT_OBSERVED -> TERMINAL
              |              |
              +-----------> AMBIGUOUS
any pre-effect invalid input -> REJECTED
```

Terminal duplicate returns only the stored bounded receipt. It never invokes the worker again.
CLAIMED can be retried once before the phase cutoff when no matching container/effect exists.
APPLYING is never blindly replayed: exact container identity and accepted evidence must prove
adoption; otherwise it becomes AMBIGUOUS and requires explicit recovery.

Atomic write contract:

1. same-directory random temp opened with `O_CREAT|O_EXCL|O_NOFOLLOW`;
2. mode `0600`, canonical JSON plus newline;
3. flush and `fsync(file)`;
4. `os.replace(temp, fence.json)`;
5. `fsync(parent)` and metadata read-back.

Disk-full, parse, metadata, fsync or read-back failure occurs before side effect or yields
AMBIGUOUS; it is never converted to success.

Lock order:

```text
/run/lock/kiwoom-stock-shadow-fence.lock
  -> /run/lock/kiwoom-stock-shadow.lock
  -> container/evidence operation
```

C* activation document performs a bounded stale-generation precheck, obtains fence lock, creates
the claim, then obtains the existing activation lock. After obtaining both authoritative state and
the rollout binding are revalidated before worker execution. No path acquires the locks in reverse.
Evidence export obtains only the activation lock and never the fence lock.

Terminal host records are kept 45 days. Authority is never pruned. Pruning happens only under the
fence lock through another durable atomic replacement.

### C* SSM documents

Create two new documents; keep the incumbent document unchanged during development.

- `KiwoomStock-ShadowCStarActivation`
  - allowed actions `continuous|stop` only;
  - exact generation/schedule/occurrence/session/release/tuple parameters;
  - invokes fence then existing worker;
  - no telemetry page action.
- `KiwoomStock-ShadowEvidenceExport`
  - manifest/page readback only;
  - exact session/release/occurrence bounds;
  - cannot start, stop, cleanup or mutate fence state.

Separate document names allow IAM to prevent observer from calling activation.

### Submitter and observer

`deploy/shadow_cstar_submitter.py` is the only component permitted to call the C* activation
document. It validates payload, performs the session/occurrence transaction, sends one exact
command per delivery attempt and records any known command ID. Ambiguous API response remains
AMBIGUOUS; Scheduler retry may submit another command, but the host fence permits one effect.

`deploy/shadow_cstar_observer.py` consumes SSM status events and reconcile invocations. It:

- conditionally advances command/runtime/closure state;
- reconciles due open occurrences even if the event was missed;
- validates bounded invocation output;
- may call only the exact evidence-export document when approved;
- archives content-addressed evidence and emits fixed metrics/Slack messages;
- never starts/stops/replays/replaces a session.

Evidence export resolves the occurrence's immutable release intent before sending the
exact evidence document. The host wrapper delegates to the canonical named-volume
`telemetry-export-page` path with the incumbent lock FD, `network none`, and
`read-only` mounts; the evidence page is capped at 4,096 bytes so its base64 JSON
envelope remains within the 12,288-byte SSM output bound. A separate, explicit
evidence-only recovery can move `ALERTED` to `EVIDENCE_PENDING` up to three times
when the activation is already `SUCCESS/STOPPED`; it cannot reopen activation or
issue a worker command.

Reconcile schedules are bounded presence/closure checks rather than an unbounded recovery loop.

## Disabled-by-default IaC

New `deploy/aws/shadow-cstar-scheduler.yaml.example` contains:

- one versioned `AWS::Scheduler::ScheduleGroup`;
- start and stop schedules, exact timezone, flexible window OFF;
- submitter and observer Lambda function/version/alias;
- Scheduler, Event-rule and reconciliation DLQs;
- C* DynamoDB table with TTL and due-closure GSI;
- SSM command/invocation status EventBridge rule;
- bounded observer reconciliation schedules;
- private versioned S3 evidence bucket, retained on stack deletion;
- retained Lambda log groups and custom/AWS service alarms;
- least-privilege Scheduler, submitter and observer roles.

Defaults:

```text
EnableActivationSchedules=false
EnableObserverRule=false
EnableReconciliationSchedules=false
SubmitterReservedConcurrency=0
ObserverReservedConcurrency=0
AlertMode=metrics-only
```

Stack creation therefore cannot activate a session.

IAM boundaries:

- Scheduler trust: `scheduler.amazonaws.com`, exact `aws:SourceAccount`, exact schedule-group
  `aws:SourceArn`; invoke exact Lambda aliases and send only to exact DLQs.
- Submitter: exact table read/conditional writes, exact C* activation document + exact EC2
  `ssm:SendCommand`, namespace-constrained metrics and logs. No list/get evidence, secrets,
  Scheduler mutation or `iam:PassRole`.
- Observer: exact table query/update, `GetCommandInvocation` and bounded list APIs where SSM
  requires `Resource:*`, exact S3 evidence prefix, metrics/logs, optional exact Slack secret.
  If approved, `ssm:SendCommand` is limited to the evidence-export document + exact instance.
- Neither role has GitHub dispatch, activation schedule mutation, rollout, broker, order, account,
  EC2 lifecycle or IAM mutation permissions.

Scheduler retry is bounded by the phase cutoff. Recommended start maximum age is 480 seconds with
two retries; stop is 900 seconds with two retries. DLQ is alert-only and has no replay consumer.

## Implementation write set

New files:

- `deploy/shadow_cstar_contract.py`
- `deploy/shadow_cstar_submitter.py`
- `deploy/shadow_cstar_observer.py`
- `deploy/build_shadow_cstar_package.py`
- `deploy/ec2/shadow_schedule_fence.py`
- `deploy/ssm/shadow-cstar-activation-document.yaml`
- `deploy/ssm/shadow-evidence-export-document.yaml`
- `deploy/aws/shadow-cstar-scheduler.yaml.example`
- corresponding `tests/deployment/test_shadow_cstar_*.py` and
  `test_shadow_schedule_fence.py`

Expected modifications:

- C* artifact rollout/bootstrap and exact contract checker paths;
- `.github/workflows/ci.yml` for package/contract tests;
- rollout workflow only when C* artifacts can be installed without arming them;
- current scheduling/current-state/notification/IAM runbooks.

The existing activation workflow/document and GitHub missing-run detector are not modified in the
first implementation slices. Continuous/stop manual recovery is redesigned through the same fence
before cutover; it must not remain as an unfenced bypass.

## Implementation sequence

| Phase | Deliverable | Verification | Rollback unit | Completion |
|---|---|---|---|---|
| P1 | pure identity/lease/state contracts | canonical/hash/strict schema/KST/state tests | new module/tests | deterministic fixtures pass |
| P2 | standalone host fence | duplicate/stale/crash/disk-full/fsync/lock-order tests | fence module/tests | one effect per occurrence proven |
| P3 | new C* SSM documents and disabled artifact install | document checker + host temp-path simulation | new docs/install paths | incumbent bytes unchanged |
| P4 | submitter and cloud ledger adapter | transaction races, retry, pre/post-send crash, late/no-session | Lambda alias RC 0 | exact activation call only |
| P5 | observer/reconciler/evidence | event loss/order, SSM terminal, export, S3/Slack failure | rule/schedules disabled | no recovery wiring |
| P6 | deterministic package and disabled IaC | ZIP hash, template defaults/IAM/forbidden capabilities | unapplied template | all triggers disabled |
| P7 | architect/reviewer/verifier bundle | full targeted tests, checker, failure matrix, Docker where safe | phase commits | C3/C4 reachable RED documented |
| P8 | AWS apply/validator/cutover | stack/IAM read-back, package/doc/host tuple, GitHub drain, corrected SSM invocation EventBridge pattern, EventBridge pair read-back | break-before-make runbook | stop/evidence payload PASS; automatic delivery on next real SSM invocation pending |

Do not mix structure, functionality, AWS apply and cutover in one commit or rollout.

## Failure and validation matrix

| Scenario | Required result |
|---|---|
| Scheduler duplicate/Lambda retry | multiple commands allowed; one host effect and duplicate receipt |
| Lambda crash before send | no command; bounded retry |
| response lost after send | AMBIGUOUS/another command possible; host effect remains one |
| DDB conditional race | one session/occurrence lease |
| old generation propagation | host reject before effect |
| host disk full/fsync failure | no effect or AMBIGUOUS, never success |
| reboot at CLAIMED | one pre-cutoff retry only |
| reboot at APPLYING | exact adoption or manual AMBIGUOUS; no blind replay |
| SSM Agent offline/failure | submission success not promoted to runtime success |
| status event lost/duplicate/out of order | reconciler closes once; normal terminal never reopens; bounded evidence-only recovery is explicit |
| holiday | exact zero-cycle CLOSED; stop closes same lease without false absent failure |
| late start/stop or missing stop lease | no SSM; durable rejection and alert |
| S3/Slack failure | runtime result retained; closure ALERTED |
| actual open day | 08:50 occurrence, first tick >=09:00, deadline, 15:35 stop, same lease and evidence |

P1–P7 can close unit/integration design but not D5. Scheduler registration is C3 and complete
submission→host→observer is C4; P8 requires validator evidence.

## Break-before-make cutover

1. Create green stack with every trigger disabled and Lambda concurrency zero; exact read-back.
2. Install C* host artifacts without arming authority; read back incumbent and C* bytes.
3. Run separately approved one-time shadow-only preflight and failure injection.
4. Drain GitHub scheduled/manual activations, SSM commands, host lock and active session.
5. Remove/disable GitHub `schedule:` and read back that no scheduled owner remains.
6. Arm matching cloud/host generation. Any mismatch stops cutover.
7. Enable observer and reconciliation first; they still cannot activate.
8. Enable stop then start schedule and read back exact pair before the next occurrence.
9. Validate first market-day start/stop/evidence/Slack closure under one session lease.

Rollback order:

1. disable C* start/stop;
2. drain Scheduler retries/DLQs and all known/ambiguous SSM commands;
3. reconcile host CLAIMED/APPLYING and running container without blind cleanup;
4. retire/disarm C* generation;
5. revalidate incumbent tuple/rollout;
6. restore GitHub schedule last and read back zero overlap.

An availability gap is acceptable. Two active owners are not.

## Confirm required before P1 implementation

Recommended defaults:

1. late start cutoff `08:58:59 KST`, Scheduler maximum age 480 seconds;
2. S3 Object Lock Governance + versioning, 400-day default retention;
3. pre-created AWS Secrets Manager Slack webhook used only by observer;
4. observer may invoke only `KiwoomStock-ShadowEvidenceExport` on the exact EC2 instance.

Choosing metrics-only instead of Slack or prohibiting evidence export leaves parity incomplete and
keeps cutover NO-GO; it does not block P1–P4 implementation.
