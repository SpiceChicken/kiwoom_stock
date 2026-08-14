# Shadow session scheduling

This runbook defines the market-hours-only shadow schedule. It never enables
broker orders, account reads, OAuth revoke, reports, S3, Gemini, or runtime
Slack notifications.

## Schedule

The protected activation workflow has two weekday schedules:

- `50 23 * * 1-5` UTC = 08:50 KST: start `shadow-continuous`.
- `35 6 * * 1-5` UTC = 15:35 KST: issue `stop` for the same daily activation.

GitHub Actions schedule delivery may be delayed. The worker therefore waits
before its first cycle until 09:00 KST and enforces the absolute 15:30 KST
close independently of the scheduler. The 15:35 stop removes the exited
container and validates terminal evidence.

The implementation is in
`.github/workflows/cd-shadow-worker-activation.yml`. Manual
`workflow_dispatch` remains available for controlled recovery.

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

## Acceptance evidence

For the next open session, accept the schedule only when:

- the first runtime cycle is at or after 09:00 KST;
- telemetry transitions through `ENTRY`, `EXIT_ONLY`, and `CLOSED` boundaries;
- cycle spacing is at least 60 seconds and `db_reopens = cycles - 1`;
- 15:35 stop produces valid terminal cleanup evidence;
- activation Slack status is `DELIVERED`;
- every runtime side-effect flag remains `false`.
