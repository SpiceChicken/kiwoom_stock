# Shadow session scheduling

This runbook defines the market-hours-only shadow schedule. It never enables
broker orders, account reads, OAuth revoke, reports, S3, Gemini, or runtime
Slack notifications.

## Schedule

The protected activation workflow has two weekday schedules:

- `50 23 * * 0-4` UTC = 08:50 KST on the following day: start
  `shadow-continuous` Monday-Friday KST.
- `35 6 * * 1-5` UTC = 15:35 KST: issue `stop` for the same daily activation.

GitHub Actions schedule delivery may be delayed. The worker therefore waits
before its first cycle until 09:00 KST and enforces the absolute 15:30 KST
close independently of the scheduler. The 15:35 stop removes the exited
container and validates terminal evidence.

The implementation is in
`.github/workflows/cd-shadow-worker-activation.yml`. Manual
`workflow_dispatch` remains available for controlled recovery.

## 현재 개장일과 사전 점검

현재 기준일은 2026-08-15 (토)이며 2026-08-17 (월)은 광복절 대체공휴일이다.
따라서 다음 실제 KRX 개장일은 2026-08-18 (화)다. cron은 평일 calendar time을
예약할 뿐 한국 거래소 휴장일을 자동으로 제거하지 않으므로, start workflow가
실행되더라도 calendar guard가 `CLOSED/calendar-closed`로 fail-closed 하는지
먼저 확인한다. 휴장일을 우회해 수동 `continuous`를 시작하지 않는다.

개장 전 순서는 다음과 같다.

1. [`current-state.md`](current-state.md)의 source/image/build tuple과 GitHub
   protected variables를 byte-for-byte 대조한다.
2. 직접 SSH로 host disk, Docker image/container inventory, worker/validator/
   binding hash와 SSM Agent health를 read-back한다. 사람용 shell에는
   `aws ssm start-session`을 사용하지 않는다.
3. 08:50 KST admission 후 첫 safe tick이 09:00 KST 이전에 실행되지 않는지
   확인한다.
4. 15:30 KST worker deadline과 15:35 KST stop/evidence 경로를 유지한다.

호스트에 별도 systemd/cron timer를 추가하지 않는다. 현재 SSH는 preflight·복구
transport이고, protected GitHub workflow가 schedule과 SSM automation의 SSOT다.

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
