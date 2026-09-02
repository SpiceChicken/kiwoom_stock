# ADR: C* Shadow schedule single SSOT

## Status

Accepted and applied — 2026-08-22.

AWS stack, SSM documents, EC2 host authority, EventBridge schedules, observer rule와
reconciliation schedule에 적용되었다. 현재 active schedule SSOT는
EventBridge Scheduler의 `cstar-g000001` pair다.

## Context

Cutover 전 Shadow session은 GitHub Actions weekday schedule이 exact release tuple을
검증하고 AWS SSM Run Command로 단일 EC2 worker를 시작·중지했다. 이 incumbent 경계는
immutable provenance, GitHub OIDC, host lock, bounded runtime evidence와
post-completion audit를 제공했지만 routine market clock으로는 유지하지 않는다.

반면 2026-08-17~21 예정 schedule 10건은 성공 0건이었고 GitHub schedule 생성·queue
지연과 protected job 대기, SSM/runtime failure가 함께 관찰됐다. GitHub schedule을
장기 market clock으로 유지할 근거는 부족하다. EC2 systemd timer는 외부 dispatch
dependency를 줄이지만 host stop, disk, clock, unit과 local evidence를 한 failure domain에
둔다.

EventBridge Scheduler는 host와 독립된 managed clock, IANA timezone, bounded retry와 DLQ를
제공한다. 그러나 Scheduler가 SSM `SendCommand`를 직접 호출하면 command ID와 node terminal,
runtime evidence를 orchestration ledger에 안전하게 결합하기 어렵고 at-least-once delivery의
중복을 durable하게 차단할 수 없다.

## Decision

최종 목표를 **C***로 정의한다.

```text
EventBridge Scheduler pair — 유일한 schedule clock owner
  -> submitter Lambda — occurrence/session/release claim
  -> exact SSM activation document
  -> EC2 root-owned durable occurrence fence
  -> existing bounded Shadow worker

SSM terminal event ─┐
reconcile schedule ─┴-> observer Lambda
                       -> exact evidence validation/export
                       -> immutable evidence archive
                       -> metrics/alarms/Slack receipt
```

### Local session independence

The automatic execution path has no dependency on a developer PC, an IAM user
session, Session Manager, or SSH. EventBridge Scheduler assumes its service
execution role, the submitter and observer use their Lambda execution roles, and
SSM reaches the single EC2 host through the instance profile and SSM Agent. A
local `aws login` session is permitted only for one-time provisioning,
deployment, or optional human read-back; its expiry must not stop, restart, or
alter a scheduled occurrence. If no human session is available, the durable
ledger, host fence, evidence bucket, CloudWatch metrics/alarms, and the approved
notification path remain the system-owned sources of truth.

### Schedule ownership

- start와 stop은 같은 versioned Schedule Group의 schedule pair다.
- `ScheduleExpressionTimezone=Asia/Seoul`, flexible window는 `OFF`다.
- CloudFormation parameter의 기본값은 activation/reconciliation schedule 모두
  `DISABLED`이며, cutover read-back 후 두 activation schedule과 observer/
  reconciliation trigger를 명시적으로 `ENABLED`로 전환했다.
- EventBridge scheduled Rule은 legacy이므로 신규 사용하지 않는다.
- EC2에는 Shadow systemd/cron timer를 설치하지 않는다.
- GitHub schedule과 EventBridge activation schedule을 동시에 enable하지 않는다.

### Submission adapter

Scheduler target은 direct SSM universal target이 아니라 immutable Lambda alias다. submitter는
다음 일만 수행한다.

1. Scheduler context의 exact schedule ARN, scheduled time, execution ID, attempt number와
   configured schedule generation을 검증한다.
2. KST session date와 canonical activation ID를 파생한다.
3. start에서는 current approved release를 그 날짜의 immutable session lease로 claim한다.
4. stop에서는 같은 session lease를 읽어 start와 동일한 release/activation ID를 사용한다.
5. cloud occurrence ledger를 conditional write한 뒤 exact instance/document/parameter로
   `SendCommand`를 호출하고 command ID를 기록한다.

submitter는 broker, account, order, revoke, report, S3 cleanup 또는 Slack capability를 갖지
않는다.

### Two independent durable boundaries

Cloud DynamoDB ledger와 host local fence는 역할이 다르며 같은 저장소로 합치지 않는다.

- cloud ledger는 schedule delivery, session lease, submission attempt, command ID와 observer
  closure를 소유한다.
- host fence는 delayed/duplicate/old-generation command가 실제 worker side effect를 반복하지
  못하게 한다.
- 기존 GitHub missing-run detector table은 detection-only이므로 C* activation ledger로
  재사용하지 않는다.

Host는 기존 `/run/lock/kiwoom-stock-shadow.lock`을 authoritative transaction lock으로
유지한다. bounded input 검증과 fast stale-generation check 후 lock을 잡고, lock 안에서
root-owned current generation/release intent와 occurrence record를 다시 검증해 atomic claim한다.
쓰기 방식은 same-directory temporary regular file, owner `root:root`, mode `0600`, file
`fsync`, `os.replace`, parent directory `fsync`로 고정한다. symlink와 hard-link metadata는
거부한다.

Terminal duplicate는 저장된 bounded receipt만 재반환하고 worker를 다시 호출하지 않는다.
in-progress 또는 crash-unknown duplicate는 container identity와 evidence로 복구 가능성이
증명되지 않으면 fail closed한다. local `flock`만을 generation fence로 간주하지 않는다.

### Completion observer

SSM Run Command status events는 빠른 신호로만 사용한다. best-effort event가 누락될 수 있으므로
정시 reconciler가 DynamoDB의 expected occurrence와 stored command ID를 기준으로 terminal을
재조회한다.

observer는 다음 권한을 갖지 않는다.

- activation 또는 stop document 실행
- replacement/recovery dispatch
- EventBridge schedule enable/update
- live trading, account 또는 broker capability

대용량 telemetry 회수에 SSM이 필요하면 별도의 read-only evidence-export document를 사용한다.
observer role은 그 document와 exact instance에만 `ssm:SendCommand`할 수 있고 activation
document에는 권한이 없다.

### Evidence and notification

- accepted runtime evidence, invocation diagnostic, telemetry, schedule observation, notification
  receipt를 동일 occurrence identity로 묶는다.
- evidence는 content hash를 포함해 off-host S3에 저장한다.
- evidence는 S3 Object Lock Governance 400일 보존 계약을 사용하며 observer role에는
  overwrite/delete/bypass 권한을 주지 않는다.
- Slack은 기존 fixed redacted message 계약을 재사용할 수 있지만 현재 cutover의
  `metrics-only` 경계에서는 webhook secret을 등록하지 않는다.
- Slack failure는 evidence closure failure지만 host cleanup/recovery command를 자동 실행하지
  않는다.

### Blue/green generations

schedule generation과 application release를 분리한다.

- schedule generation은 Scheduler/Lambda/SSM/fence protocol 세대를 식별한다.
- release intent는 source SHA, image digest, build run, Compose hash, worker/validator/fence 및
  document hashes를 식별한다.
- session lease는 start 시 선택한 release intent를 stop/evidence closure까지 동결한다.
- schedule pair를 in-place 변경하지 않는다. 새 generation은 disabled blue pair로 만들고 exact
  read-back과 C3/C4 검증 후 old generation을 폐기한다.

## Rejected alternatives

### GitHub schedule을 최종 SSOT로 유지

현재 parity가 가장 높아 cutover 전 incumbent로는 유지하지만 documented delay/drop과 실제
weekday 실패 때문에 최종 market clock으로 채택하지 않는다.

### EC2 systemd timer

local dispatch는 짧지만 schedule, execution, secret, local evidence가 단일 EC2 failure domain에
묶인다. independent missing-run evidence를 얻기 위해 다시 외부 plane을 추가해야 하므로 최종
SSOT로 채택하지 않는다.

### Scheduler direct SSM target

`SendCommand` API acceptance가 runtime success가 아니고 command ID를 occurrence ledger에
직접 기록할 adapter가 없다. recurring start/stop의 동일 KST session lease와 response-loss
reconciliation도 불완전해 채택하지 않는다.

### 두 scheduler의 병행 fallback

host lock은 concurrent execution을 직렬화할 뿐 delayed old owner를 폐기하지 않는다. 두 active
owners는 availability가 아니라 duplicate/stale command source이므로 금지한다.

## Consequences

장점:

- GitHub runner availability와 queue delay를 routine market clock에서 제거한다.
- host down과 schedule clock failure를 분리한다.
- command submission, node terminal, runtime acceptance, evidence closure를 별도 상태로
  관찰할 수 있다.
- cloud ledger와 host fence가 at-least-once duplicate를 서로 다른 failure domain에서 방어한다.

비용과 단점:

- Lambda, DynamoDB, SQS DLQ, S3, alarms와 별도 IAM 경계가 추가된다.
- SSM Agent와 EC2/runtime failure는 계속 남는다.
- status event가 best-effort이므로 reconciler와 command-history read가 필수다.
- release registration, session lease, telemetry export와 rollback이 현재 GitHub workflow보다
  많은 명시적 상태를 가진다.

## Non-goals

- 실거래, 주문, 취소, 계좌 조회 활성화
- EC2 추가 생성 또는 multi-host HA
- KRX holiday를 Scheduler resource에서 제거
- 자동 recovery/replacement command
- existing GitHub missing-run detector와 activation ledger 통합
- runtime application Slack/report/S3 capability 활성화

## Activation gate

적용 당시 다음 gate를 순서대로 통과했다.

1. host occurrence fence와 cloud ledger의 단위 검증
2. disabled CloudFormation stack 및 IAM/resource read-back
3. host C* artifact 설치·authority arm 및 tuple read-back
4. GitHub scheduled run drain과 workflow disable read-back
5. EventBridge observer/reconciliation 활성화 read-back
6. EventBridge start/stop pair enable read-back
7. 첫 실제 개장일 start/stop/evidence/observer closure는 후속 acceptance evidence

rollback은 EventBridge를 먼저 disable하고 retry, DLQ, in-flight SSM과 host occurrence를
reconcile한 뒤 마지막에 GitHub schedule을 복구한다.

## Applied parameters

구현 전 결정이 필요했던 값은 cutover read-back 기준 다음과 같이 확정됐다.

- late start hard cutoff: `08:58:59 KST`
- evidence retention: S3 Object Lock Governance 400일
- Slack: `metrics-only`; webhook secret은 등록하지 않음
- observer: exact `KiwoomStock-ShadowEvidenceExport` document만 사용

단계별 write set, 상태 전이, 검증과 rollback은
[C* 구현 계획](shadow-scheduler-cstar-implementation-plan.md)이 소유한다.
