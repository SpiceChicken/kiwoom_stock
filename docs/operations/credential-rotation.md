# 키움 자격증명 회전 및 사고 대응

## 공통 안전 상태

모든 회전·폐기·사고 대응은 worker와 주문 capability가 `DISARMED`인 상태에서 시작한다. 자동 재시작을
끄고 새로운 인증·조회·주문을 시작하지 않는다. key 또는 token 값을 terminal, log, ticket, chat,
문서에 출력하지 않는다.

외부 키움 콘솔에서 key를 발급·폐기하거나 revoke API를 호출하는 작업은 외부 상태 변경이다. 승인된
운영자와 별도 실행 승인이 필요하며, 이 repository의 config/test 명령은 이를 대신하지 않는다.
Token 만료·refresh·HTTP retry와 `REVOKED`/`REJECTED`/`UNKNOWN` 판정의 SSOT는
[credential lifecycle runbook](../auth/credential-lifecycle-runbook.md)이다. 이 문서는 operator가
static key pair를 교체하고 사고를 통제하는 순서만 정의한다.
Key custody, delivery, inventory, CI/repository governance는
[Kiwoom credential management](../security/kiwoom-credential-management.md)가 소유한다.

## 계획 회전

1. 변경 window, mock/prod 환경, owner, rollback owner를 기록한다. prod key를 mock에 사용하지 않는다.
2. worker를 중지하고 `DISARMED`를 확인한다. 기존 process를 둔 채 파일을 교체하지 않는다.
3. 기존 owner의 명시적 revoke를 lifecycle runbook대로 한 번 수행하고 typed 결과를 기록한다.
   `UNKNOWN`을 성공으로 바꾸거나 자동 재시도하지 않는다.
4. 승인된 키움 관리 경로에서 새 pair를 발급한다. 값은 운영자가 승인된 secret 입력 화면 또는 실행
   호스트의 보호된 입력 경로로 직접 넣으며 중간 채널에 복사하지 않는다.
5. 기존 directory와 다른 repository-external hardened directory에 두 파일을 함께 준비한다. 한 파일씩
   기존 위치를 수정하지 않는다.
6. owner/mode/link/content와 pair generation preflight를 값 출력 없이 확인한다.
7. `KIWOOM_CREDENTIALS_DIR` 또는 배포 provider의 version reference를 새 pair로 원자적으로 전환한다.
8. 먼저 config/preflight-only 경로를 실행한다. 이는 network나 실제 인증 성공을 증명하지 않는다.
9. 별도 승인된 격리 mock validator에서 인증 lifecycle과 `expires_dt` timezone, revoke semantics를
   확인한다. 검증 전 상태는 `NO_GO`다.
10. 검증 증거가 있고 위험이 해소된 경우에만 별도 first-activation 승인을 진행한다. Key 존재만으로
    `ACTIVE`나 주문 capability를 만들지 않는다.
11. rollback window 종료 후 기존 key를 키움 관리 경로에서 폐기하고 기존 secret version/file을
    복구 불가능하게 제거한다. 삭제는 정확한 target을 다시 확인하고 승인된 secret system 절차를 따른다.
12. 인벤토리에는 날짜, owner, environment, status, 값 없는 evidence만 갱신한다.

rollback은 이전 파일을 즉시 되살리는 동작이 아니다. 이전 key가 아직 유효하고 사고 징후가 없으며
rollback이 명시적으로 승인된 경우에만 이전 provider version으로 전환한다. 유출 의심 key는 rollback
후보가 아니며 먼저 revoke/rotate한다.

## 유출 의심 또는 secret-scan finding

1. 즉시 `DISARMED / NO_GO`로 전환하고 자동 재시작과 배포를 멈춘다.
2. finding 원문, 일부 값, hash, prefix/suffix를 채팅이나 ticket에 복사하지 않는다. rule ID, file path,
   commit ID, 발견 시각처럼 값 없는 증거만 기록한다.
3. 실제 key일 가능성이 있으면 Git 정리보다 먼저 키움 관리 경로에서 revoke/rotate한다. history rewrite는
   credential을 무효화하지 않는다.
4. 계정 권한과 키움 측 audit/접속 기록을 승인된 운영자가 검토한다. 의심 주문이나 계정 변화는 별도 사고
   대응 절차로 격리하며 자동 주문/청산을 수행하지 않는다.
5. leak surface를 확인한다: Git history, fork, CI log/cache/artifact, container image/layer, local clone,
   terminal transcript, chat/image, backup.
6. repository에서 값을 제거한다. 이미 push된 history rewrite와 force-push는 파괴적 작업이므로 영향
   분석과 명시적 사용자 승인을 받은 별도 작업으로 수행한다.
7. redacted full-history scan과 필요한 hosting-side scan을 다시 실행한다. scanner가 없거나 일부 history만
   검사했으면 `BLOCKED`이지 PASS가 아니다.
8. 새 pair는 계획 회전 절차를 따르고, root cause와 재발 방지 조치가 닫힐 때까지 first activation을
   승인하지 않는다.

## 폐기와 계정 종료

1. consumer와 process가 0인지 확인하고 `DISARMED`를 유지한다.
2. lifecycle runbook대로 token owner를 폐기하고 명시적 revoke를 한 번 수행해 typed 결과를 기록한다.
3. 운영자가 키움 관리 경로에서 key pair를 폐기하고 상태를 확인한다.
4. secret provider의 active/previous version과 승인된 host file을 정확한 target으로 제거한다.
5. 인벤토리 status를 `revoked`로 변경하되 값이나 secret-derived fingerprint를 남기지 않는다.
6. 값 없는 audit evidence와 보존 기간만 기록한다.

## 현재 activation blocker

- 실제 Docker target에서 UID/GID `10001:10001`과 파일 owner/mode/readability가 미검증;
- host source가 absolute/repository-external인지 보장하는 launcher가 없음;
- 실제 mock token의 `expires_dt` timezone 의미와 revoke 응답이 미검증;
- 승인된 배포 대상과 중앙 secret manager가 미정;
- 실제 주문 capability와 first activation은 승인되지 않음.

위 항목이 닫히기 전 현재 상태는 `BLOCKED / NO_GO / DISARMED`다.
