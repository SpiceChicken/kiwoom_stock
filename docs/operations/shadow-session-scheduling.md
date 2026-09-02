# Shadow session scheduling

This runbook defines the market-hours-only shadow schedule. It never enables
broker orders, account reads, OAuth revoke, reports, S3, Gemini, or runtime
Slack notifications.

## C* 단일 SSOT 전환 산출물

기존 GitHub owner를 drain/disable한 뒤, 다음 C* 경계를 단일 SSOT로 적용했다.

EventBridge Scheduler -> submitter Lambda -> KiwoomStock-ShadowCStarActivation
-> EC2 root-owned durable fence -> existing kiwoom-shadow-worker
-> SSM status observer/reconciler -> exact evidence-export document -> S3/Slack

deploy/shadow_cstar_contract.py가 schedule generation, KST session date,
occurrence identity, immutable release/session lease와 ledger state transition의
SSOT다. delivery execution_id/attempt_number는 retry evidence일 뿐 occurrence
identity에 들어가지 않는다. start는 08:50 KST, 08:58:59 KST cutoff과 480초/2회
retry를 사용하고 stop은 15:35 KST, 15:50:59 KST cutoff과 900초/2회 retry를
사용한다. stop은 active release pointer를 재조회하지 않고 start가 만든 daily
session lease의 release를 사용한다.

운영 슬롯을 수동 테스트에 재사용하지 않는다. 동일한 production schedule ARN,
phase와 `scheduled_time`은 `execution_id`나 retry attempt가 달라도 같은
occurrence ID를 만들기 때문에, 해당 슬롯의 수동 Lambda 호출은 실제 Scheduler
delivery를 중복 실행하는 대신 idempotency 경로에서 차단될 수 있다. 수동 재현은
별도 test schedule/generation과 격리된 날짜를 사용하고, 이미 사용된 운영 슬롯은
DynamoDB 원장이나 호스트 fence에서 삭제·덮어쓰지 않고 audit evidence로 보존한 뒤
다음 미사용 개장일 슬롯에서 자동 acceptance를 수행한다.

현재 C* stack, SSM documents, EC2 fence 설치/arm, GitHub schedule 제거와
EventBridge cutover가 완료되었다. generation은 `cstar-g000001`이며 실제
activation clock owner는 EventBridge Scheduler다. 첫 개장일의 submission→host
effect→observer→evidence closure를 추가 acceptance evidence로 확인한다.

이전 GitHub activation workflow의 weekday schedules는 cutover 시 제거되었다.
원격 workflow는 `workflow_dispatch` 선언만 보존하지만 activation job은
`if: ${{ false }}`로 비활성화되어 second owner가 아니다.

현재 EventBridge Scheduler pair는 다음과 같다.

- `cron(50 8 ? * MON-FRI *)` / `Asia/Seoul`: start `continuous` admission.
- `cron(35 15 ? * MON-FRI *)` / `Asia/Seoul`: stop for the same daily lease.

Scheduler delivery may be delayed. The worker therefore waits
before its first cycle until 09:00 KST and enforces the absolute 15:30 KST
close independently of the scheduler. The 15:35 stop removes the exited
container and validates terminal evidence.

The previous GitHub activation workflow and its post-completion audit remain only
as disabled rollback evidence. They are not a current activation path, and their
run IDs, cron observations, or artifact commands must not be used as current
schedule evidence. The active implementation is the C* submitter/observer and
EC2 fence. Repository branch policy, immutable tuple validation, exact SSM
documents, and the broker-order deny boundary remain in force.

## 현재 개장일과 사전 점검

cron은 평일 calendar time을 예약할 뿐 한국 거래소 휴장일을 자동으로 제거하지
않는다. 따라서 start occurrence가 전달되더라도 runtime calendar guard가
`CLOSED/calendar-closed`로 fail-closed 한다. 휴장일을 우회해 수동
`continuous`를 시작하지 않는다.

개장 전 자동 수행 경계는 다음과 같다.

1. C* Submitter는 DynamoDB `CONTROL#CSTAR/RELEASE` pointer와 참조
   `RELEASE#<release_id>/META`의 exact release intent를 검증한다. C* runtime은
   repository-level `KIWOOM_SHADOW_SCHEDULE_*` 변수를 읽지 않는다. 해당 변수는
   아래의 비활성화된 구형 GitHub activation workflow와 historical audit에만
   보존된다.
2. 직접 SSH는 사후 read-back과 복구에만 사용한다. 사람용 shell에는
   `aws ssm start-session`을 사용하지 않는다.
3. 08:50 KST admission 후 worker가 첫 safe tick을 09:00 KST 이후에만
   실행한다.
4. 15:30 KST worker deadline과 15:35 KST stop/evidence 경로를 유지한다.

호스트에 별도 systemd/cron timer를 추가하지 않는다. 현재 SSH는 preflight·복구
transport이고, EventBridge Scheduler와 C* cloud ledger/host fence가 SSOT다.
비활성화된 GitHub workflow를 fallback으로 병행하지 않는다. 두 scheduler를 함께
켜면 duplicate owner, tuple drift, concurrency 충돌이 생길 수 있다.

EventBridge Scheduler, observer rule, reconciliation schedule, IAM, DLQ와
immutable tuple 전달 경계가 C* stack으로 적용되었다. start/stop pair는
generation `cstar-g000001`과 host authority에 결속되어 있으며, rollback 시에는
먼저 C* start/stop을 disable하고 in-flight command/evidence를 drain한다.

## 릴리스 ledger와 스케줄의 원자적 admission

스케줄이 `ENABLED`인 것만으로는 실행 준비가 끝난 상태가 아니다. Submitter는
다음 세 항목이 모두 존재하고 서로 정확히 일치할 때만 SSM을 호출한다.

- `GEN#<generation>/META`: 현재 start/stop schedule ARN과 protocol hash
- `RELEASE#<release_id>/META`: host rollout과 image의 immutable tuple
- `CONTROL#CSTAR/RELEASE`: 위 release를 가리키는 `ACTIVE` pointer

따라서 CloudFormation stack을 처음 만들거나 release를 바꿀 때는 template의
activation/reconciliation schedule을 먼저 `false`로 적용하고
`ActivationScheduleState=DISABLED`를 유지한 뒤, 다음 fail-closed bootstrap을
실행한다. ledger read-back 후에만 `ActivationScheduleState=ENABLED`로 바꾼다.

```bash
export AWS_DEFAULT_REGION=ap-northeast-2
export AWS_PROFILE=kiwoom-cstar-release-rotator

./.venv/bin/python deploy/bootstrap_shadow_cstar_ledger.py \
  --region ap-northeast-2 \
  --table-name '<CSTAR_TABLE_NAME>' \
  --generation '<CSTAR_GENERATION>' \
  --protocol-sha256 '<PROTOCOL_SHA256>' \
  --source-sha '<SOURCE_SHA>' \
  --image-digest '<IMAGE_DIGEST>' \
  --compose-shadow-sha256 '<COMPOSE_SHADOW_SHA256>' \
  --worker-sha256 '<WORKER_SHA256>' \
  --validator-sha256 '<VALIDATOR_SHA256>' \
  --shadow-document-sha256 '<SHADOW_DOCUMENT_SHA256>' \
  --rollout-attempt-id '<ROLLOUT_ATTEMPT_ID>' \
  --check
```

`--check`는 읽기 전용이다. 실제 적용은 동일한 인자로 `--check`만 제거한다.
이 명령의 일상 profile은 `kiwoom-cstar-release-rotator`이며, root/Admin은 해당
역할을 최초 bootstrap할 때만 사용한다. C* 실행 결과의 관측·증적 확인에는
`kiwoom-cstar-observer`를 사용하고, 두 역할 모두 root/Admin 세션을 요구하지
않는다.
도구는 start/stop schedule의 ARN·timezone·expression·target generation을 먼저
검증하고, 필요하면 두 schedule을 `DISABLED`로 만든다. 그 뒤 누락된 ledger item만
조건부 transaction으로 추가하고 exact read-back을 통과한 경우에만 두 schedule을
`ENABLED`로 복귀시킨다. 어느 단계라도 실패하면 schedule은 disabled 상태로 남아
반복 실행이나 반쪽 release를 막는다. 이미 다른 generation/release가 존재하면
덮어쓰지 않고 중단한다.

Bootstrap and release rotation also read the Compose, worker, validator, and SSM
document blobs directly from the supplied immutable `source_sha` Git revision
before any AWS write. A supplied hash that was calculated from a dirty checkout
or a different revision therefore fails locally as an artifact-tuple mismatch.

Submitter가 admission을 거부하면 이제 `REJ#<occurrence_id>/META`에
`REJECTED`, 거부 사유, schedule context와 `ssm_sent=false`를 저장하고
`Kiwoom/ShadowCStar:cstar_activation_rejected` metric을 기록한다. 이 기록과
Lambda 구조화 로그가 다음 개장일의 “스케줄 호출은 있었지만 SSM이 없었다”를
구분하는 기준이다. 이 경로는 실거래 capability를 추가하지 않는다.

2026-08-27 KST start acceptance에서는 Scheduler와 Submitter Lambda가 정상적으로
SSM command를 제출했지만, activation document의 첫 셸 명령이 Linux 기본
`/bin/sh`에서 Bash 전용 `set -Eeuo pipefail`을 사용해 exit code 2로 종료되었다.
호스트 fence·worker·Kiwoom 호출 전 단계의 실패라 외부 주문 side effect는 없었다.
문서는 POSIX 호환 `set -eu`로 수정되어 AWS 기본/latest v2로 read-back되었고,
계약 검사와 회귀 테스트도 추가했다. 이미 생성된 occurrence를 중복 실행하지
않으며 다음 평일 스케줄에서 수정 문서의 SSM 성공과 host/evidence closure를
확인한다.

이후 Observer v6를 배포해 reconciliation이 `PENDING/IN_PROGRESS` command의
`GetCommandInvocation` 상태를 직접 읽도록 보강하고, terminal activation/evidence
failure에 `cstar_observer_alerted` metric과 metrics-only alarm을 연결했다. status
event가 누락되어도 기존 occurrence ID에 상태를 적용하며, command index의 audit 행과
occurrence `META` 행이 함께 조회되는 경우에도 `META`만 사용한다. 오늘 실패 occurrence는
새 activation command 없이 `FAILED/ALERTED`로 확정했다.

2026-08-31 KST start failure는 위 control-plane 경계가 정상 동작한 뒤 host
runtime의 첫 safe tick 전에 발생한 `MarketDataCollectionError`였다. Scheduler,
Submitter, DynamoDB admission, SSM delivery, disk와 fence가 직접 원인이 아니며,
기존 sentinel에 operation/kind가 없어 세부 endpoint는 확정할 수 없었다. PR #144의
merged release는 allowlisted market-data failure kind/operation을 redacted
sentinel과 terminal evidence에 추가했다. production-check, protected rollout,
host read-back, C* release rotation과 schedule tuple read-back을 완료했으며,
현재 start/stop/reconciliation은 `ENABLED`다. 개장 전 release 적용이므로 실제
worker activation은 발생시키지 않았다.

Observer v7은 terminal activation failure를 자동으로 한 번 조회해
allowlisted market-data `failure_diagnostic`만 occurrence `META`와 보호된
failure notification에 기록한다. 이 보완은 raw output이나 credential을
저장하지 않으며, Lambda alias와 CloudFormation package read-back까지
완료했다.

2026-09-01 KST automatic start acceptance에서는 08:50 start의
`SESSION#`/`OCC#`/SSM command, 09:00 이후 첫 safe tick, host fence effect와
장중 `healthy` continuous runtime을 확인했다. 현재 start occurrence는
`SUCCESS/ACCEPTED/OPEN`이며, 15:35 stop와 observer evidence closure를 같은
release lease로 확인하는 단계가 남아 있다. 실패 시 중복 activation을 발행하지
않고 occurrence, Lambda/SSM evidence, host terminal output과 DLQ/metric을 먼저
보존한다.

정상 activation `Success`가 observer alert metric을 발생시키던 false positive는
PR #148에서 수정했다. Observer v8은 `Failed`/`TimedOut` 등 terminal failure와
evidence failure만 `cstar_observer_alerted` 대상으로 처리하며, 정상 Success는
원장을 수용 상태로만 진행한다. 정상 Success가 경보를 만들지 않는 회귀 테스트,
전체 CI와 CloudFormation `UPDATE_COMPLETE`, alias `live` version 8을 확인했다.

2026-08-25 KST에는 이 rejection audit 자체가 Submitter role의 누락된
`dynamodb:PutItem` 권한 때문에 실패했다. 그 결과 start/stop은 Lambda retry 후
종료되었고 SSM은 호출되지 않았다. PR #122에서 `dynamodb:PutItem`을 C* table
ARN에만 추가하고 IaC checker·회귀 테스트를 통과시킨 뒤 CloudFormation에
적용했다. 다음 개장일에는 `SESSION#`, `OCC#`, SSM command와 host evidence가
연속적으로 생성되는지 확인하며, 오류가 재발하면 수동 재실행하지 않고 Lambda
오류·DynamoDB ledger·SSM command evidence를 먼저 보존한다.

schedule delivery가 지연되더라도 worker가 장 시작 전 safe tick을 막고,
15:30 KST deadline을 자체 적용한다. schedule은 평일 시각을 예약할 뿐
한국거래소 휴장일을 cron에서 제거하지 않으므로, runtime calendar guard가
`CLOSED/calendar-closed`로 fail-closed 한다. 휴장일을 우회해 수동
`continuous`를 시작하지 않는다.

## Bounded execution contract

Continuous cycles remain 60 seconds apart. The process cap is seven hours as
an outer safety limit, but the effective deadline is the earlier of that cap
and 15:30 KST on the admitted date. A start after the close fails closed
without constructing the runtime. A closed KRX calendar still returns the
normal `CLOSED/calendar-closed` result without a cycle.

One regular session can emit at most 390 one-minute cycles. The standalone
evidence validator accepts up to 512 bounded records so the cycle evidence
and terminal record remain within one finite input budget.

## Legacy GitHub activation tuple (disabled)

The following repository-level Actions variables belong only to the disabled
legacy GitHub activation workflow and its historical audit:

- `KIWOOM_SHADOW_SCHEDULE_SOURCE_SHA`
- `KIWOOM_SHADOW_SCHEDULE_IMAGE_DIGEST`
- `KIWOOM_SHADOW_SCHEDULE_BUILD_RUN_ID`

They are not a C* schedule input and changing them cannot start, stop, or alter a
C* occurrence. If the legacy workflow is ever reintroduced, its source SHA must
be the immutable released commit used by its runtime image and host rollout, the
image digest must carry that same source revision, and the build run ID must be
the successful `cd-production-check.yml` candidate for that tuple. Runtime,
deployment-template, or package changes still require a new production check,
exact rollout, and C* release rotation before the next market-day schedule.
Documentation-only commits do not change the C* runtime release.

Keep the tuple unchanged from the 08:50 start through the 15:35 stop and the
observer's post-completion evidence closure. Update it only after both the start
and stop occurrences have closed with bounded `PASS` evidence, the previous
session has been stopped, and a new immutable rollout has completed.
Changing either tuple value while an audit is pending makes that audit fail
closed; do not repair this by selecting a different artifact or dispatching a
replacement run without a separate confirmed recovery decision. Missing,
stale, or mismatched values fail before OIDC/SSM execution or during audit.

### Release correction and rotation

The initial ledger bootstrap is not a release correction mechanism. If an
immutable release contains a wrong artifact hash, preserve that release and use
`deploy/rotate_shadow_cstar_release.py` to add a new exact release intent and
conditionally move `CONTROL#CSTAR/RELEASE` to it. The tool never edits or deletes
an existing `RELEASE#<release_id>/META` item.

Run `--check` first. Use `--apply` only after the current market-day start/stop
occurrences have reached terminal closure (`CLOSED` or `ALERTED`) and no worker
is running. The apply path also refuses to run during the 09:00-16:00 KST
weekday market window, temporarily disables both C* schedules, performs the
conditional DynamoDB
transaction, reads the pointer back, and re-enables both schedules only after
the read-back succeeds. If any mutation fails, schedules remain disabled for
manual recovery.

The current C* release is recorded by the DynamoDB `ACTIVE` pointer and its
release metadata; exact values are read through the observer boundary. Do not
copy a historical GitHub tuple from the production-check guide into the C*
ledger.

현재 비활성 legacy GitHub schedule tuple:

| 항목 | 값 |
|---|---|
| Source SHA | repository variable `KIWOOM_SHADOW_SCHEDULE_SOURCE_SHA` |
| Image digest | repository variable `KIWOOM_SHADOW_SCHEDULE_IMAGE_DIGEST` |
| Build run | repository variable `KIWOOM_SHADOW_SCHEDULE_BUILD_RUN_ID` |

위 세 값은 disabled legacy workflow가 사용하는 값이다. C* release를 운영
대상으로 전환할 때는 production check와 exact rollout을 성공시킨 뒤
`rotate_shadow_cstar_release.py`로 새로운 exact release intent를 추가하고
`CONTROL#CSTAR/RELEASE` pointer를 조건부 전환한다.

휴장일 admission은 continuous worker가 `CLOSED/calendar-closed` zero-cycle
terminal을 남기고 종료하며, 호스트 제어 스크립트는 해당 컨테이너를 제거한 뒤
성공적인 fail-closed evidence를 반환한다. 따라서 휴장일에 매매를 시도하지 않고
예약 occurrence도 불필요한 runtime failure로 분류하지 않는다. Observer/reconciliation
closure는 이 경우에만 `runtime_status=CLOSED`, `cycles=0`, `db_reopens=0`,
database `false`, decision telemetry 부재, database side effect `false`, 그리고
diagnostic의 exact `CLOSED/calendar-closed` zero-cycle terminal을 정상 closure로
허용한다. Terminal activation summary는 의도적으로 `http_attempts=null`인
축약값을 제공한다. Auditor는 이 값과 diagnostic을 exact 검증한 뒤에만 bounded
audit 결과의 `http_attempts`를 `0`으로 정규화한다. Open-session continuous의
`PASS`/one-cycle/six-attempt/database/decision mapping 계약은 그대로다.

## Acceptance evidence

For the next open session, accept the schedule only when:

- the first runtime cycle is at or after 09:00 KST;
- telemetry transitions through `ENTRY`, `EXIT_ONLY`, and `CLOSED` boundaries;
- cycle spacing is at least 60 seconds and `db_reopens = cycles - 1`;
- 15:35 stop produces valid terminal cleanup evidence;
- when status notification is enabled, activation Slack status is `DELIVERED`;
- broker order, account read, OAuth revoke, runtime notification, report, and
  S3 side-effect flags remain `false`; the isolated paper database identity is
  present.

`DELIVERED` is observer evidence, not an assumption that the old application
Slack path or live trading has been restored. A missing or failed notification is
a shadow acceptance failure and remains visible in the evidence.

### Legacy GitHub audit material

The former GitHub artifact-audit procedure is retained in Git history only. It
is not a current verification command after the C* cutover. Current evidence is
closed by the C* observer and reconciliation path below.

### Current C* closure

The current closure path is independent of GitHub Actions:

```text
EventBridge start/stop occurrence
  -> submitter ledger + SSM command
  -> host fence/runtime terminal
  -> SSM status event or five-minute reconciliation
  -> observer evidence validation/export
  -> occurrence closure, metrics, and optional protected notification
```

The cloud ledger and host fence are the authoritative evidence boundaries. A
successful scheduler delivery or SSM command submission alone is not runtime
success. Acceptance requires the same daily session lease for start and stop,
valid terminal cleanup, evidence closure, and all side-effect flags remaining
false. Missing event delivery is handled by reconciliation; no component
automatically dispatches a replacement activation.

Human read-back uses the non-admin `kiwoom-cstar-observer` profile. It is limited
to the exact C* ledger, evidence prefix, schedules, SSM result APIs, EC2 health,
CloudWatch alarms/metrics, and the three scheduler DLQs. Root/Admin is required
only for the one-time IAM bootstrap that creates this role; it is not part of
the scheduled execution or routine acceptance path.

After a successful stop command, the observer durably records the evidence
command ID on the occurrence. Reconciliation polls that evidence command while
the occurrence is `EVIDENCE_PENDING`, so a missing evidence status event cannot
leave the session permanently unverified. Non-terminal evidence statuses remain
pending; terminal evidence failures become `ALERTED` and emit the same bounded
observer alert metric without retrying activation.

When the observer confirms a terminal activation failure or evidence failure, it first
persists the occurrence as `ALERTED` and then emits the
`Kiwoom/ShadowCStar:cstar_observer_alerted` metric. The corresponding
metrics-only CloudWatch alarm provides a bounded detection signal without
retrying activation or enabling broker capabilities.

The former GitHub missing-run classifier remains available for historical
diagnostics and does not own the current C* occurrence ledger. Its fixture tests
are network-free and it has no dispatch, rerun, SSM, EC2, broker, account, or
order operation. It must not be used as a replacement activation mechanism.

The former GitHub missing-run Lambda and its apply-later CloudFormation template
are not part of the active C* deployment. They remain historical design
material only; no GitHub Runs API credential, replacement dispatcher, or
additional detector is enabled by this runbook.

The former `shadow-missing-run-detector.yaml.example` is an apply-later
CloudFormation boundary and is not an active C* resource. Its schedules remain
disabled and it is retained only as historical design material.
Do not build or enable this legacy detector as part of the current schedule
recovery path. C* package and template verification is owned by the
`shadow-scheduler-cstar-implementation-plan.md` and the active C* stack.

For current read-back, use the exact occurrence/session identifiers from the AWS
C* ledger and the redacted evidence export. Do not use historical GitHub run
IDs, GitHub cron observations, or the disabled workflow's audit artifacts as
current evidence.
