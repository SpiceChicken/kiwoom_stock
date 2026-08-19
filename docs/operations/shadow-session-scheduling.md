# Shadow session scheduling

This runbook defines the market-hours-only shadow schedule. It never enables
broker orders, account reads, OAuth revoke, reports, S3, Gemini, or runtime
Slack notifications.

## Schedule

The activation workflow has two weekday schedules:

- `50 23 * * 0-4` UTC = 08:50 KST on the following day: start
  `shadow-continuous` Monday-Friday KST.
- `35 6 * * 1-5` UTC = 15:35 KST: issue `stop` for the same daily activation.

GitHub Actions schedule delivery may be delayed. The worker therefore waits
before its first cycle until 09:00 KST and enforces the absolute 15:30 KST
close independently of the scheduler. The 15:35 stop removes the exited
container and validates terminal evidence.

The implementation is in
`.github/workflows/cd-shadow-worker-activation.yml`. The scheduled job runs
from `main` without a human environment-review gate; the repository branch
policy, immutable tuple validation, OIDC role, exact SSM document, and broker
order deny boundary remain in force. Manual `workflow_dispatch` remains
available for controlled recovery.

## 현재 개장일과 사전 점검

cron은 평일 calendar time을 예약할 뿐 한국 거래소 휴장일을 자동으로 제거하지
않는다. 따라서 start workflow가 실행되더라도 runtime calendar guard가
`CLOSED/calendar-closed`로 fail-closed 한다. 휴장일을 우회해 수동
`continuous`를 시작하지 않는다.

개장 전 자동 수행 경계는 다음과 같다.

1. schedule은 repository-level `KIWOOM_SHADOW_SCHEDULE_*` tuple을 사용해
   승인 없이 exact source/image/build를 검증한다.
2. 직접 SSH는 사후 read-back과 복구에만 사용한다. 사람용 shell에는
   `aws ssm start-session`을 사용하지 않는다.
3. 08:50 KST admission 후 worker가 첫 safe tick을 09:00 KST 이후에만
   실행한다.
4. 15:30 KST worker deadline과 15:35 KST stop/evidence 경로를 유지한다.

호스트에 별도 systemd/cron timer를 추가하지 않는다. 현재 SSH는 preflight·복구
transport이고, GitHub Actions schedule과 SSM automation이 SSOT다.

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

## Schedule tuple registration

Before enabling the schedule, register these repository-level Actions
variables from the exact release that passed production-check and shadow
rollout:

- `KIWOOM_SHADOW_SCHEDULE_SOURCE_SHA`
- `KIWOOM_SHADOW_SCHEDULE_IMAGE_DIGEST`
- `KIWOOM_SHADOW_SCHEDULE_BUILD_RUN_ID`

The source SHA must be the current `main` commit, the image digest must carry
the same source revision, and the build run ID must be the successful
`cd-production-check.yml` candidate run for that tuple. The worker, validator,
SSM document, and `compose.shadow.yaml` hashes are calculated from that exact
source by the workflow; the host rollout binding must already contain the same
worker/document artifact set.

Keep the tuple unchanged from the 08:50 start through the 15:35 stop. Update it
only after the previous session has been stopped and a new immutable rollout
has completed. Missing, stale, or mismatched values fail before OIDC/SSM
execution.

The current registered tuple is recorded in [`current-state.md`](current-state.md).
Do not copy a historical tuple from the production-check guide.

현재 자동 schedule tuple:

| 항목 | 값 |
|---|---|
| Source SHA | `c1a7e2735a985ae661366623e9760eb904897c7e` |
| Image digest | `ghcr.io/spicechicken/kiwoom_stock@sha256:96379a88c2861b15a924ef70829b1dbeb1ad289da2893401dec334f6595f7d52` |
| Build run | `32217767456` |

이 tuple은 현재 `main`과 EC2 rollout binding 및 activation image와 동일하다.
새 release를 운영 대상으로 전환할 때는 production check와 exact rollout이
성공한 뒤 세 schedule 변수를 함께 갱신한다.

## Acceptance evidence

For the next open session, accept the schedule only when:

- the first runtime cycle is at or after 09:00 KST;
- telemetry transitions through `ENTRY`, `EXIT_ONLY`, and `CLOSED` boundaries;
- cycle spacing is at least 60 seconds and `db_reopens = cycles - 1`;
- 15:35 stop produces valid terminal cleanup evidence;
- when status notification is enabled, activation Slack status is `DELIVERED`;
- every runtime side-effect flag remains `false`.

`DELIVERED` is workflow evidence, not an assumption that the old application
Slack path or live trading has been restored. A missing or failed notification is
a shadow acceptance failure and remains visible in the evidence.
