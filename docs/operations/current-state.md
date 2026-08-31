# 현재 운영 기준선

이 문서는 2026-09-01 (KST) 기준으로 실제 호스트와 저장소에 반영된 운영
상태를 기록하는 기준 문서다. 과거 bootstrap 기록이나 재생성 예시와 현재
호스트 상태가 다를 때는 이 문서와 AWS read-back을 우선한다. 이 문서는 공개
저장소에 있으므로 live host의 주소·네트워크·리소스 식별자는 기록하지 않는다.
정확한 값은 AWS/private operator inventory에서 read-back한다.

## 운영 범위

- 실제 매매·주문·취소·계좌 조회는 운영하지 않는다.
- 현재 허용 범위는 bounded shadow worker, 설정 전용 `--check-config`, 읽기
  전용 검증과 redacted evidence뿐이다.
- 사람의 EC2 관리 접속은 직접 SSH를 사용한다. 사람용 Session Manager
  `start-session`은 사용하지 않는다.
- GitHub Actions의 보호된 production-check, shadow rollout, shadow activation
  자동화는 기존 SSM Command document 경계를 유지한다. 이것은 사람의 SSH
  접속을 SSM으로 되돌린다는 뜻이 아니다.
- Slack은 보호된 shadow 상태 알림의 선택적 경로로만 사용한다. 현재 C*는
  `metrics-only`이며 webhook secret을 등록하지 않았다. 향후 활성화하더라도
  실제 전송 성공은 redacted `DELIVERED` evidence가 있을 때만 인정하며,
  애플리케이션 runtime Slack 또는 live trading 복구를 의미하지 않는다.

## 종료된 기존 live 호스트

clean-rebuild 전의 기존 호스트는 2026-08-15 KST에 종료했고, 현재 운영 대상이
아니다. 과거 인스턴스·주소·네트워크 리소스 식별자는 공개 기준 문서에서 제거했다.
철거 여부와 보존 리소스는 AWS/private operator inventory에서만 확인한다.

## 현재 단일 운영 호스트

기존 live host를 대체한 단일 clean-rebuild 호스트다. 호스트 자체의
cloud-init completion marker/SSH/Docker/SSM 검증, 설정 전용 production-check와
shadow artifact rollout/read-back을 완료했으며, shadow worker와 실제 credential은
시작하거나 사용하지 않았다.

| 항목 | 현재 값/판정 |
|---|---|
| Instance | 단일 EC2 운영 대상 (정확한 ID는 AWS/private inventory) |
| Region | `ap-northeast-2` (Seoul) |
| Public/private address | 공개 문서에 기록하지 않음; AWS read-back으로 확인 |
| VPC / Subnet / ENI / EIP / Security group | 공개 문서에 기록하지 않음; exact network inventory로 확인 |
| Root volume | 8 GiB gp3, encrypted; exact volume ID는 private inventory |
| Key pair | repository 밖의 승인된 키; 이름과 경로는 공개 문서에 기록하지 않음 |
| State file | repository 밖, mode `0600`; 경로는 공개 문서에 기록하지 않음 |
| Host validation | `cloud-init-complete` marker present, `sshd -t` valid, Docker active, snap `amazon-ssm-agent` active; bounded shadow session completed with terminal evidence |
| Kiwoom REST API allowlist | 고정 운영 egress 주소가 등록됨; exact 주소는 private inventory |

2026-08-27 KST 직접 SSH read-back에서 현재 호스트의 `cloud-init status`는
`error - done`으로 남아 있음을 확인했다. 최초 user-data가 `/run/sshd`를 생성하기
전에 `sshd -t`를 실행해 `cloud-final`이 실패한 historical 상태이며, 현재 호스트에는
completion marker가 있고 `/run/sshd`, SSH, Docker, SSM Agent가 정상이다. 저장소의
현재 [cloud-init script](../../deploy/ec2/cloud-init-ubuntu-24.04.sh)는 이 순서를
수정해 `/run/sshd`를 먼저 생성한다. 기존 호스트에서 cloud-init을 clean/re-run하지
않은 이유는 bootstrap 전체를 재실행해 서비스와 호스트 설정을 다시 변경할 위험이
있기 때문이다. 이 historical 상태는 현재 C* Scheduler 실행을 차단하지 않지만,
호스트 재생성 acceptance에서는 `cloud-init status`와 completion marker를 모두
확인한다.

2026-08-19 KST 첫 bounded shadow session 후 직접 SSH read-back 결과는 다음과 같다.
컨테이너는 worker deadline에 exit code 0으로 종료되었고,
`kiwoom-stock-shadow_kiwoom-shadow-data` named volume과 shadow DB는 보존되었다.
첫 세션 paper ledger의 거래 레코드는 0건이었다.
호스트의 `/`는 6.8 GiB 중 1.6 GiB 여유(77% 사용), inode 사용률은 21%였다.
Docker inventory는 이미지 4개·활성 컨테이너 0개·볼륨 1개·build cache 0개이며,
Docker root 사용량은 2.228 GB였다. SSM은
`snap.amazon-ssm-agent.amazon-ssm-agent.service`가 active/running임을 확인했다.
이는 설정·운영 read-back 증적이며 shadow activation 또는 실제 API 호출 증적이
아니다.

EC2 console의 `KeyName`이 비어 있어도 현재 SSH가 끊긴다는 뜻은 아니다. 승인된
공개 키가 `ubuntu`의 `authorized_keys`에 설치되어 있으며 SSH daemon은
password/kbd-interactive/root login을 막고 public-key login만 허용한다. 키를
교체하거나 SG의 관리 `/32`를 바꿀 때는 먼저 새 SSH 연결을 별도로 확인한다.

2026-08-24 KST 관리자 read-back에서 repository 밖 PEM 개인키와 host
`authorized_keys`의 공개키 지문이 일치했고, 양쪽 파일은 regular file·link count
1·mode `0600`이었다. 실제 SSH는 `ubuntu`로 성공했으며 `sshd -T`는
`PermitRootLogin no`, `PasswordAuthentication no`,
`KbdInteractiveAuthentication no`, `PubkeyAuthentication yes`,
`X11Forwarding no`, `AllowUsers ubuntu`를 반환했다. Security Group inbound는
관리 주소의 TCP 22 단일 `/32`이고 IPv6 SSH ingress는 없다. exact 주소·resource
ID·key fingerprint는 private operator inventory에만 둔다.

## 자동화 target 전환 상태

### C* 전환 구현 상태

2026-08-22 KST 기준 C* 단일 SSOT 전환이 AWS와 단일 운영 호스트에 적용되었다.
순수 occurrence identity/session lease/state contract, root-owned durable host
fence, 별도 C* activation/evidence SSM documents, submitter/observer adapter,
deterministic Lambda ZIP, EventBridge/DynamoDB/S3/IAM/DLQ CloudFormation stack이
동일 generation으로 read-back되었다.

기존 GitHub activation workflow는 schedule trigger를 제거하고 job을 비활성화했다.
실제 clock owner는 EventBridge Scheduler이며, 다음 개장일의 start/stop 실행과
observer/reconciliation closure evidence를 별도 확인한다.

| 항목 | 현재 상태 |
|---|---|
| C* stack / EventBridge schedules | `kiwoom-shadow-cstar`, start/stop/reconciliation ENABLED, generation `cstar-g000001` |
| C* host fence | `/var/lib/kiwoom-stock/shadow-schedule/fence.json` 설치·root-owned·armed |
| C* SSM documents | `KiwoomStock-ShadowCStarActivation` default/latest v2, `KiwoomStock-ShadowEvidenceExport` default/latest v2, both Active |
| C* submitter/observer | submitter Lambda alias `live` version 7, observer alias `live` version 7, observer EventBridge rule ENABLED, reconciliation 5분 |
| 실제 schedule owner | EventBridge Scheduler; legacy GitHub activation job은 disabled |

2026-08-24 KST schedule incident와 remediation read-back:

- EventBridge Scheduler의 start/stop invocation은 발생했지만, 당시 submitter는
  active release/ledger admission 실패를 영속 기록하지 않아 SSM command·session·
  occurrence가 생성되지 않았다. 이는 scheduler clock 자체의 중단이 아니라
  ledger admission과 rejection observability가 부족했던 문제였다.
- Submitter는 이제 거부를 `REJ#<occurrence_id>/META`에 기록하고 structured Lambda
  log와 `Kiwoom/ShadowCStar:cstar_activation_rejected` metric을 남긴다. 전용
  rejection alarm, Lambda log group(30일), 올바른 Logs/CloudWatch IAM도 적용했다.
- CloudFormation에 `ActivationScheduleState`를 추가해 ledger 준비 전에는
  schedule이 `DISABLED`인 상태만 배포할 수 있게 했다. immutable release
  bootstrap은 schedule을 끄고 generation/release/pointer를 조건부 seed/read-back한
  뒤에만 다시 켠다.
- `AWS::Lambda::Version`은 immutable package key를 Description에 결속해 package가
  바뀌면 `live` alias가 새 version으로 이동한다. 현재 submitter alias와 observer
  alias는 모두 version 7이다.

2026-08-27 KST DynamoDB transaction remediation read-back:

- 원인은 테이블 키나 IAM 권한이 아니라 `boto3.resource("dynamodb").Table`의
  backing client에 붙은 변환기와 `TypeSerializer`의 이중 적용이었다. 입력의
  `PK`/`SK`는 코드상 `S`였지만 실제 HTTP body에서 `M(Map)`로 변환되어
  `PK expected: S actual: M`이 발생했다.
- Submitter ledger는 resource-backed client에는 native Python 값을 전달하고,
  standalone low-level client에만 `AttributeValue`를 직렬화하도록 경계를
  고정했다. ledger bootstrap도 같은 규칙으로 정렬했다.
- 실제 Lambda version 7의 무거래 `stop/no-session` probe가
  `REJECTED_NO_SESSION`으로 정상 종료했고, `REJ#<occurrence_id>/META`의
  `REJECTED` 감사 레코드와 `ssm_sent=false`를 확인했다. 이 probe에서는 SSM
  command·EC2·Kiwoom 호출이 발생하지 않았다.
- 동일 경계를 Botocore 실제 변환 이벤트로 검증하는 회귀 테스트와 전체 테스트를
  통과했다. Submitter Lambda error alarm은 `OK`이며 Submitter/Observer/
  Reconciliation DLQ의 가시·비가시 메시지는 모두 0이다. 세 DLQ에는 이제
  `ApproximateNumberOfMessagesVisible >= 1` metrics-only alarm도 연결되어,
  Scheduler/EventBridge delivery 실패가 보관만 되고 조용히 지나가지 않도록
  한다. 자동 DLQ 재처리나 Slack 전송은 여전히 활성화하지 않았다.

2026-08-27 KST start execution incident and remediation read-back:

- 08:50 KST EventBridge Scheduler delivery와 Submitter Lambda version 7 실행은
  성공했고, `SESSION#2026-08-27` 및 start occurrence가 기록되었으며 SSM command가
  단일 운영 호스트로 제출되었다.
- SSM command는 호스트 fence에 도달하기 전에 exit code 2로 실패했다. 원인은
  `aws:runShellScript`가 Linux 기본 `/bin/sh`로 실행되는데 activation document가
  Bash 전용 `set -Eeuo pipefail`을 첫 줄에 사용한 셸 호환성 결함이었다. 이 실패로
  worker, Kiwoom API, broker order side effect는 발생하지 않았다.
- 저장소의 activation document를 POSIX 호환 `set -eu`로 수정하고, 동일 결함의
  재발을 막도록 C* SSM contract checker와 회귀 테스트를 보강했다. 로컬 검증 후
  AWS `KiwoomStock-ShadowCStarActivation` default/latest를 v2로 갱신하고,
  실제 문서 내용에서 `set -eu`를 read-back했다.
- 증거 수집 문서도 동일한 전용 deployer 역할로 AWS `KiwoomStock-ShadowEvidenceExport`
  default/latest v2로 갱신했으며, live 문서 내용의 SHA-256과 저장소 canonical
  문서가 일치함을 read-back했다. 전용 역할은 이 문서의 버전 갱신과 C* schedule
  read-back만 허용하고, worker 실행·SendCommand·parameter·log·IAM 변경은 허용하지 않는다.
- 오늘 start occurrence는 이미 `SUBMITTED`로 기록된 command이므로 중복 start를
  수동 발행하지 않는다. 다음 평일 acceptance에서 v2 문서의 SSM 성공, host fence,
  paper shadow terminal 및 observer closure를 순서대로 확인한다.

2026-08-27 KST observer reconciliation remediation:

- Observer는 기존에 reconciliation 대상 occurrence를 조회만 하고 SSM command의
  실제 상태를 읽지 않아, status event가 누락되면 `PENDING/UNKNOWN/OPEN` 상태가
  남을 수 있었다. 또한 command index에는 occurrence `META`와 command audit 행이
  함께 존재했다.
- Observer v6는 v5의 `PENDING/IN_PROGRESS` command reconciliation을 유지하면서
  terminal activation/evidence failure metric과 alarm을 추가했다. `PENDING/IN_PROGRESS` command에 대해
  `GetCommandInvocation`을 조회하고, reconciliation이 이미 보유한 occurrence ID를
  명시해 상태를 적용한다. command index fallback도 `META` 행만 선택하도록
  보강했다.
- stop 성공 후 evidence command ID도 occurrence에 영속 기록하며, `EVIDENCE_PENDING`
  상태에서는 해당 command를 reconciliation으로 조회한다. evidence status event가
  누락되거나 비종료 상태여도 조기 `ALERTED` 처리나 무한 미검증 상태가 발생하지
  않으며, terminal evidence failure는 동일한 observer alert metric으로 드러난다.
- 오늘 실패 command를 새 SSM 명령 없이 reconciliation 경로로 검증했고,
  occurrence 원장을 `FAILED/FAILED/ALERTED`로 확정했다. 이후 같은 항목은 due
  대상에서 제거된다.
- Observer가 활성화 또는 evidence SSM command의 terminal failure를 확인하면
  원장을 먼저 `ALERTED`로 저장한 뒤 `Kiwoom/ShadowCStar:cstar_observer_alerted`
  metric을 발행한다. 이 metric에는 metrics-only CloudWatch alarm이 연결되어
  알림 전송 실패가 상태 저장을 되돌리거나 재실행을 유발하지 않는다.

2026-08-31 KST start execution incident and 2026-09-01 remediation read-back:

- EventBridge Scheduler delivery, Submitter Lambda, DynamoDB admission, SSM
  command submission, host disk 상태는 정상으로 확인되었다. 호스트 컨테이너는
  08:50 start 뒤 첫 safe tick 전에 종료했으며, SSM command는 timeout이 아니라
  exit code 1로 끝났다.
- 직접 원인은 runtime의 `MarketDataCollectionError`였다. 따라서 scheduler,
  AWS session, Docker 용량, IAM 또는 host fence가 이번 실패의 직접 원인은
  아니었다. 당시 배포된 sentinel이 예외 종류만 보존하고 operation/kind를
  보존하지 않아, 하위 Kiwoom endpoint까지는 historical evidence만으로 확정할
  수 없다.
- PR #144에서 `MarketDataCollectionError`에 대해 allowlisted
  `error_kind`(`empty`/`fetch`/`timeout`/`parse`/`malformed`)와
  allowlisted `error_operation`만 redacted sentinel·terminal evidence·진단
  artifact에 남기도록 보강했다. raw response, exception text, credential은
  계속 기록하지 않는다.
- merged `main`의 production-check와 protected shadow rollout을 다시 통과했고,
  단일 운영 호스트의 worker/validator/binding read-back이 새 release와
  일치했다. AWS C* active release pointer와 repository schedule tuple도 같은
  release를 가리키며, start/stop/reconciliation은 각각 `ENABLED` 상태다.
- 이번 배포는 개장 전 수행되어 worker activation을 발생시키지 않았다. 현재
  EC2는 실행 상태이고 SSM Agent는 `Online`이며, 다음 평일 08:50 automatic
  start → 09:00 이후 첫 safe tick → 15:35 stop/evidence가 최종 acceptance
  gate다. 실거래 capability는 계속 비활성이다.
- Observer v7은 terminal activation failure의 SSM stdout/stderr를 자동으로
  조회하되, 정확히 allowlisted 된 market-data `category/kind/operation`만
  `failure_diagnostic`으로 occurrence `META`에 저장·알림에 반영한다.
  oversized·충돌·미등록 값과 raw provider output, exception text, credential은
  폐기한다. CloudFormation `UPDATE_COMPLETE`, `live` alias version 7 및
  package hash read-back을 완료했다.

2026-08-25 KST post-repair acceptance에서 start/stop Scheduler delivery는
정상적으로 Submitter Lambda에 도달했지만, Lambda role의 C* DynamoDB 정책에
`dynamodb:PutItem`이 빠져 session/occurrence와 rejection audit을 저장하지 못했다.
각 schedule은 최초 호출과 두 번의 retry 후 종료되었고 SSM command·EC2 worker·
브로커 side effect는 발생하지 않았다. PR #122에서 `PutItem`을 C* table ARN에만
허용하도록 IaC와 회귀 검사를 보강했고, 2026-08-25 KST CloudFormation
`UPDATE_COMPLETE` 및 IAM policy simulation/read-back을 완료했다. 현재 start/stop
schedule은 기존대로 `ENABLED`이며, 실제 자동 start→host effect→stop closure는
다음 개장일 acceptance에서 확인한다.

현재 자동 실행 경계는 다음 SSOT로 정렬되어 있다. 정확한 release 값은 공개
문서에 복제하지 않고 repository Actions 변수와 AWS C* ledger에서 read-back한다.

| 항목 | 현재 read-back |
|---|---|
| Source SHA | repository variable `KIWOOM_SHADOW_SCHEDULE_SOURCE_SHA`와 AWS `ACTIVE` release |
| Production check | repository variable `KIWOOM_SHADOW_SCHEDULE_BUILD_RUN_ID` |
| Shadow rollout | AWS `ACTIVE` release의 `rollout_attempt_id` |
| Image | repository variable `KIWOOM_SHADOW_SCHEDULE_IMAGE_DIGEST`와 AWS `ACTIVE` release |
| Active release | DynamoDB `CONTROL#CSTAR/RELEASE` pointer와 참조 `RELEASE#<release_id>/META` |
| Schedule state | start/stop `ENABLED`, `Asia/Seoul`, exact 08:50/15:35 KST |

실거래·계좌 조회·주문 capability는 계속 비활성이다. 다음 평일 start/stop에서
실제 SSM submission, host fence effect, paper shadow terminal, observer evidence
closure를 확인하는 acceptance만 남아 있다. 이 acceptance 전에는 “자동 실행이
검증 완료”라고 판정하지 않는다.

AWS cutover read-back 기준:

| 항목 | 값 |
|---|---|
| Stack | `kiwoom-shadow-cstar` / `UPDATE_COMPLETE` |
| Schedule group | `kiwoom-shadow-cstar` |
| Generation | `cstar-g000001` |
| DynamoDB/S3 resources | active resources are stack-managed; exact names are private inventory |
| Host authority | `eventbridge-scheduler`, armed for generation `cstar-g000001` |

2026-08-15 KST에 GitHub production-check/shadow 자동화의 AWS target을 후보
호스트로 전환했다. 기존 호스트는 종료했으며 새 운영 호스트만 유지한다.

| 항목 | 전환 결과 |
|---|---|
| Automation target | 단일 EC2 운영 대상; exact ID/address는 AWS/private inventory |
| Production-check document | `KiwoomStock-ProductionCheck`, default/latest `3` |
| Shadow activation document | `KiwoomStock-ShadowWorker`, default/latest `5` |
| Shadow rollout document | `KiwoomStock-ShadowWorkerRollout`, default/latest `6` |
| GitHub OIDC target policies | production-check, shadow-activation, shadow-rollout 3건을 후보 ARN으로 갱신 |
| Candidate config check | attempt `31891989562`, `Configuration OK`, `production check passed` |
| Shadow activation | bounded activation 정상 deadline 종료 확인; 실제 키움 credential로 broker 주문 없음 |

AWS read-back은 새 target과 문서 기본 버전을 확인했다. 위의 초기 host
전환·설정 검증 run은 historical evidence로 보존하며, 현재 운영 release와
첫 자동 session evidence는 아래 immutable release tuple에 기록한다.

## 완료된 호스트 작업

- Docker 미사용 이미지 7개와 약 710 MB의 reclaimable layer를 정리했다.
- container/build cache와 volume은 범위를 확인한 뒤 보존했다. 운영 named
  volume을 삭제하는 `docker system prune --volumes`는 실행하지 않았다.
- root filesystem은 초기 정리 후의 현재 read-back에서 6.8 GiB 중 1.6 GiB
  여유(77% 사용)이며, inode 사용률은 21%다. Docker 이미지와 volume은 보존
  상태를 확인했고, 운영 named volume을 삭제하는 `docker system prune --volumes`는
  실행하지 않았다.
- SSH hardening을 적용하고 `sshd -t`, daemon restart, 신규 SSH 연결을
  확인했다.
- `kiwoom-production-check`를 exact release tuple로 실행해 `Configuration OK`
  및 `production check passed`를 확인했다. 이 검사는 network none,
  placeholder credential, `--check-config`만 사용했으며 외부 API·주문·Slack을
  호출하지 않았다.
- 현재 단일 운영 호스트에서 동일한 immutable tuple로 production-check attempt
  `31870050000`을 추가 검증했다. 새 target의 고정 ID·리전·compose/image/source
  hash가 일치했고, 종료 후 컨테이너가 남지 않았다.
- 종료된 기존 호스트에서 수행했던 shadow worker/validator/rollout artifact
  read-back 기록은 historical evidence로만 보존한다.
- 현재 단일 운영 호스트에는 최신 tuple의 shadow worker/validator와 canonical
  activation-document binding이 설치되어 있다. 과거 bounded session의 paper
  ledger는 보존되어 있으며, 2026-09-01 release rollout 자체에서는 worker를
  실행하지 않았다.

## 현재 immutable release tuple

실행 시점의 source/image/build tuple은 아래 repository-level Actions variable의
값이 SSOT다. 문서에 release SHA를 고정하지 않아 문서 commit 자체가 다음
schedule release를 stale하게 만들지 않도록 한다. 세 값은 rollout 성공 후
함께 read-back한다.

| 항목 | 값 |
|---|---|
| Source SHA | `KIWOOM_SHADOW_SCHEDULE_SOURCE_SHA` |
| Image | `KIWOOM_SHADOW_SCHEDULE_IMAGE_DIGEST` |
| Build run | `KIWOOM_SHADOW_SCHEDULE_BUILD_RUN_ID` |
| Compose / production Compose SHA | latest release manifest for that tuple |
| Worker / validator / shadow document SHA | latest exact shadow rollout evidence |
| Production check / rollout / activation | latest successful evidence artifacts |

이 tuple은 현재 `main` source와 동일 revision image의 production-check 및 shadow
artifact rollout/read-back 통과 tuple이어야 한다. workflow preflight가 불일치를
자동 거부한다. `production-shadow`는 main branch policy와 exact validation을
유지하며, schedule은 human reviewer 없이 수행된다.
휴장일에는 continuous worker가 zero-cycle `CLOSED/calendar-closed` terminal과
정상 cleanup evidence를 남기며, 실제 주문·계좌·외부 부수효과는 계속 비활성이다.

## 다음 실제 장 운영 창

EventBridge Scheduler가 평일 KST 시각을 예약하지만 거래소 휴장일 자체를
스케줄 레이어에서 제거하지 않으므로, runtime holiday/calendar guard와 exact
tuple preflight가 자동으로 fail-closed 경계를 담당한다.

- 08:50 KST: automatic `continuous` activation admission
- 09:00 KST 이후: 첫 safe tick 허용
- 15:30 KST: worker 자체 deadline
- 15:35 KST: automatic `stop`, exact container 제거 및 terminal evidence

이전 stale tuple을 사용하던 schedule run은 자동 수행 전에 취소했고,
현재 schedule variables는 마지막 exact rollout 성공 tuple로 갱신되어 있다. 다음 평일 schedule은
별도 human approval 없이 exact tuple preflight 후 실행된다.

과거 protected-review 대기 run들은 historical evidence로만 보존하며, 현재
schedule은 required reviewer 없이 main branch policy와 exact tuple 검증을
통과한 뒤 실행된다.

호스트에 별도의 중복 timer를 만들지 않는다. 스케줄 SSOT는
EventBridge Scheduler이며, 호스트 SSH는 preflight·복구·read-back에만 사용한다.

## 현재 남은 차단 항목

- 다음 개장일 schedule에서 IAM 수정과 2026-08-31 runtime 진단 보강 후 자동
  start/stop이 다시 수행되는지 확인할 운영 모니터링;
- 애플리케이션 runtime Slack과 별개인 보호 상태 알림의 기존 운영 채널 end-to-end
  확인;
- `apply_clean_rebuild.sh`와 두 JSON intent는 SSH key pair, TCP 22 관리 `/32`,
  cloud-init hardening과 SG read-back을 요구하는 재생성 계약으로 갱신됐다.
  승인된 key pair와 현재 preflight 관리 `/32`는 AWS read-back으로 확인했으며,
  exact 값은 공개 문서에 기록하지 않는다. 첫 실행의 CLI 옵션 결함과 IAM 조건
  revision을 수정한 뒤 resume launch와 후보 호스트 read-back을 완료했으며,
  기존 live host에는 적용하지 않았다;
- repository의 `local-operator-policy.json.example`과 AWS의
  `kiwoom-local-operator` 실제 inline policy에서 사람용 SSM session 권한을
  제거했다. 2026-08-24 KST에 canonical read-only 정책 하나로 교체하고 종료된
  host를 가리키던 session/recovery 정책과 임시 SSM 목록 정책을 삭제했다.
  `StartSession`, resume/terminate, data channel, `SendCommand`와 parameter read는
  implicit deny이며 exact live target에 대한 실제 `start-session`도 AccessDenied를
  반환했다. EC2 inventory와 SSM managed-node health read는 허용되고 SSM Agent는
  Online이므로 GitHub/C* Run Command 자동화 의존성은 유지된다;
- `kiwoom-local-provisioner` 역할·trust·inline policy와 관리자 1회 bootstrap을
  적용했다. `aws-admin` root 세션에서 role과 `KiwoomLocalProvisioner`를
  생성하고, 기존 `KiwoomLocalAssumeOperatorRole`에 provisioner AssumeRole을
  추가했다. `SignInLocalDevelopmentAccess`는 `aws login`용 exact read-back 정책으로
  유지됐으며, 최종 role은 `kiwoom-local-provisioner`다. account/ARN은
  공개 문서에 기록하지 않는다. 절차는
  [provisioner bootstrap 가이드](local-provisioner-bootstrap.md)에 기록했다;
- `kiwoom-cstar-document-deployer` 역할·trust·inline policy를 추가하고,
  `kiwoom-local-user`의 AssumeRole 대상에 exact 역할을 추가했다. 이 역할은
  `KiwoomStock-ShadowEvidenceExport` 문서의 version update/default 전환과 C*
  schedule read-back만 허용하며 worker activation, SendCommand, parameter,
  log, IAM 변경 권한은 없다. 저장소 템플릿은
  `cstar-document-deployer-*.json.example`이다;
- `kiwoom-cstar-release-rotator` 역할·trust·inline policy를 추가하고,
  `kiwoom-local-user`의 AssumeRole 대상에 exact 역할을 추가했다. 이 역할은
  C* 원장 table의 `GetItem`·`PutItem`·`UpdateItem`·`TransactWriteItems`와
  start/stop schedule의
  `GetSchedule`·`UpdateSchedule`, 그리고 두 schedule의 기존 execution role에
  한정된 `iam:PassRole`만 허용한다. 기존 release item은 수정·삭제하지 않고
  새 release item과 조건부 active pointer 전환만 수행한다. 저장소 템플릿은
  `cstar-release-rotator-*.json.example`이다;
- 새 실제 Kiwoom 인증/시세 검증은 별도 명시적 read-only window 없이는 수행하지
  않는다. 과거 read-only evidence가 live worker·계좌·주문 capability를 승인하는
  근거가 되지는 않는다.

## 근거 문서

- [로컬 AWS/SSH 접근](aws-local-access.md)
- [EC2 수동 생성 및 복구](aws-ec2-manual-setup-guide.md)
- [배포 경계](deployment-boundary.md)
- [Shadow 세션 스케줄](shadow-session-scheduling.md)
- [운영 runbook](runbook.md)
