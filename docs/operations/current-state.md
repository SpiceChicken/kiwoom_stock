# 현재 운영 기준선

이 문서는 2026-08-23 (KST) 기준으로 실제 호스트와 저장소에 반영된 운영
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
cloud-init/SSH/Docker/SSM 검증, 설정 전용 production-check와 shadow artifact
rollout/read-back을 완료했으며, shadow worker와 실제 credential은 시작하거나
사용하지 않았다.

| 항목 | 현재 값/판정 |
|---|---|
| Instance | 단일 EC2 운영 대상 (정확한 ID는 AWS/private inventory) |
| Region | `ap-northeast-2` (Seoul) |
| Public/private address | 공개 문서에 기록하지 않음; AWS read-back으로 확인 |
| VPC / Subnet / ENI / EIP / Security group | 공개 문서에 기록하지 않음; exact network inventory로 확인 |
| Root volume | 8 GiB gp3, encrypted; exact volume ID는 private inventory |
| Key pair | repository 밖의 승인된 키; 이름과 경로는 공개 문서에 기록하지 않음 |
| State file | repository 밖, mode `0600`; 경로는 공개 문서에 기록하지 않음 |
| Host validation | cloud-init complete, `sshd -t` valid, Docker active, snap `amazon-ssm-agent` active; bounded shadow session completed with terminal evidence |
| Kiwoom REST API allowlist | 고정 운영 egress 주소가 등록됨; exact 주소는 private inventory |

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
| C* SSM documents | `KiwoomStock-ShadowCStarActivation` / `KiwoomStock-ShadowEvidenceExport`, Active v1 |
| C* submitter/observer | Lambda alias `live`, observer EventBridge rule ENABLED, reconciliation 5분 |
| 실제 schedule owner | EventBridge Scheduler; legacy GitHub activation job은 disabled |

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
  activation-document binding이 설치되어 있다. 첫 자동 session 뒤 worker는
  `run-deadline`/exit code 0으로 종료했고 paper ledger는 보존되었다.

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

- 다음 개장일 schedule에서 자동 start/stop이 다시 수행되는지 확인할 운영 모니터링;
- 애플리케이션 runtime Slack과 별개인 보호 상태 알림의 기존 운영 채널 end-to-end
  확인;
- `apply_clean_rebuild.sh`와 두 JSON intent는 SSH key pair, TCP 22 관리 `/32`,
  cloud-init hardening과 SG read-back을 요구하는 재생성 계약으로 갱신됐다.
  승인된 key pair와 현재 preflight 관리 `/32`는 AWS read-back으로 확인했으며,
  exact 값은 공개 문서에 기록하지 않는다. 첫 실행의 CLI 옵션 결함과 IAM 조건
  revision을 수정한 뒤 resume launch와 후보 호스트 read-back을 완료했으며,
  기존 live host에는 적용하지 않았다;
- repository의 `local-operator-policy.json.example`에서는 사람용 SSM session
  권한을 제거했다. AWS에 이미 연결된 inline policy의 실제 교체·삭제는 별도
  관리자 IAM 변경과 read-back이 필요하며, 그 전에도 사람용 접속은 SSH만 사용한다;
- `kiwoom-local-provisioner` 역할·trust·inline policy와 관리자 1회 bootstrap을
  적용했다. `aws-admin` root 세션에서 role과 `KiwoomLocalProvisioner`를
  생성하고, 기존 `KiwoomLocalAssumeOperatorRole`에 provisioner AssumeRole을
  추가했다. `SignInLocalDevelopmentAccess`는 `aws login`용 exact read-back 정책으로
  유지됐으며, 최종 role은 `kiwoom-local-provisioner`다. account/ARN은
  공개 문서에 기록하지 않는다. 절차는
  [provisioner bootstrap 가이드](local-provisioner-bootstrap.md)에 기록했다;
- 새 실제 Kiwoom 인증/시세 검증은 별도 명시적 read-only window 없이는 수행하지
  않는다. 과거 read-only evidence가 live worker·계좌·주문 capability를 승인하는
  근거가 되지는 않는다.

## 근거 문서

- [로컬 AWS/SSH 접근](aws-local-access.md)
- [EC2 수동 생성 및 복구](aws-ec2-manual-setup-guide.md)
- [배포 경계](deployment-boundary.md)
- [Shadow 세션 스케줄](shadow-session-scheduling.md)
- [운영 runbook](runbook.md)
