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

Each run that GitHub actually starts queries only its own Actions run metadata
(`event`, `id`, `created_at`, `run_started_at`, and `head_branch`). A strict
helper accepts only the two cron/action pairs above, exact UTC timestamps, the
current run ID on `main`, and non-negative delays below 24 hours. It writes the
bounded observation to the current-run-only
`${RUNNER_TEMP}/shadow-schedule-observation-${GITHUB_RUN_ID}.json` path after
removing that path before the API call. The uploaded file contains expected,
created, and started UTC timestamps plus delivery, queue, and total-start delay
seconds.
The job summary exposes only action, expected UTC, and total delay. A valid
scheduled observation also supplies the optional Slack
`schedule_delay=<seconds>s` suffix. Before using it, the notifier binds the
artifact to the expected current run ID and event cron. A stale file or either
binding mismatch is invalid. Invalid or ambiguous input creates no accepted
observation artifact and is marked `invalid` in the summary and notification
receipt; it does not relax admission, deadline, cleanup, or other runtime
safety checks. Manual dispatch is schedule-observation `n-a`.

This in-workflow observation measures a run that started late. It cannot detect
a scheduled run that GitHub never creates or starts. The current recovery
contract remains GitHub schedule plus the existing controlled
`workflow_dispatch`; an external missing-run watchdog is not part of this
change.

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

Host systemd는 GitHub schedule을 먼저 끄고 single schedule SSOT, host lock,
immutable tuple binding, evidence, Slack, rollback을 동등하게 재검증할 때만 전환
후보다. 두 scheduler를 함께 켜면 duplicate owner, tuple drift, concurrency 충돌이
생길 수 있으므로 fallback으로 병행하지 않는다.

EventBridge는 새 AWS resource와 IAM의 owner가 승인되고 immutable tuple 전달,
SSM/notification/evidence parity, 비용과 rollback이 정의되며 C3/C4 validator를
통과할 때만 전환 후보다. 이번 변경은 systemd timer, EventBridge resource, IAM,
외부 watchdog을 생성하거나 변경하지 않는다.

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

The source SHA must be an immutable released commit on `main` and an exact
ancestor of the current control-plane `main` SHA. The image digest must carry
that source revision, and the build run ID must be the successful
`cd-production-check.yml` candidate run for that tuple. The worker, validator,
SSM document, and `compose.shadow.yaml` hashes are calculated from that exact
source by the workflow; the host rollout binding must already contain the same
worker/document artifact set. A runner-only control-plane change may advance
`main` without replacing the runtime tuple only when the runtime image,
Compose, worker, validator, and SSM document inputs are unchanged and the
workflow ancestry check remains exact. Any runtime payload change requires a
new production check, image digest, rollout, and tuple update.

Keep the tuple unchanged from the 08:50 start through the 15:35 stop. Update it
only after the previous session has been stopped and a new immutable rollout
has completed. Missing, stale, or mismatched values fail before OIDC/SSM
execution.

The current registered tuple is recorded in [`current-state.md`](current-state.md).
Do not copy a historical tuple from the production-check guide.

현재 자동 schedule tuple:

| 항목 | 값 |
|---|---|
| Source SHA | repository variable `KIWOOM_SHADOW_SCHEDULE_SOURCE_SHA` |
| Image digest | repository variable `KIWOOM_SHADOW_SCHEDULE_IMAGE_DIGEST` |
| Build run | repository variable `KIWOOM_SHADOW_SCHEDULE_BUILD_RUN_ID` |

위 세 값과 EC2 rollout binding 및 activation image가 동일한지 workflow가
매번 exact 검증한다. 새 release를 운영 대상으로 전환할 때는 production check와
exact rollout이 성공한 뒤 세 schedule 변수를 함께 갱신한다.

휴장일 admission은 continuous worker가 `CLOSED/calendar-closed` zero-cycle
terminal을 남기고 종료하며, 호스트 제어 스크립트는 해당 컨테이너를 제거한 뒤
성공적인 fail-closed evidence를 반환한다. 따라서 휴장일에 매매를 시도하지 않고
예약 workflow도 불필요한 runtime failure로 분류하지 않는다.

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

`DELIVERED` is workflow evidence, not an assumption that the old application
Slack path or live trading has been restored. A missing or failed notification is
a shadow acceptance failure and remains visible in the evidence.

### Same-run artifact audit

`deploy/audit_shadow_schedule_bundle.py` is the deterministic post-run gate.
It accepts only a completed successful scheduled run and one non-expired
artifact whose API digest, byte size, `workflow_run.id`, and
`workflow_run.head_sha` all match. Inside that single ZIP it requires the
run-ID-named schedule observation, notification receipt v2, accepted runtime
evidence, and diagnostic. A successful `continuous` audit requires one safe
cycle, six market-only HTTP attempts, a paper database identity, and no unsafe
side effects. A successful `stop` audit additionally requires the exact
`STOPPED` terminal plus the telemetry manifest and gzip export; every telemetry
row, row hash, session hash, compressed hash, source/image identity, and cycle
count is revalidated.

Use a newly created private temporary directory and project the GitHub API
responses to the exact schemas below. Fill the seven uppercase values from the
run and the registered immutable tuple; do not copy values from an unrelated
historical run.

```bash
audit_dir="$(mktemp -d /tmp/kiwoom-shadow-schedule-audit.XXXXXX)"
chmod 700 "${audit_dir}"
REPO=SpiceChicken/kiwoom_stock
RUN_ID=123456789
CONTROL_PLANE_SHA=0000000000000000000000000000000000000000
SOURCE_SHA=0000000000000000000000000000000000000000
IMAGE_DIGEST=ghcr.io/spicechicken/kiwoom_stock@sha256:0000000000000000000000000000000000000000000000000000000000000000
ACTIVATION_ID=shadow-session-YYYYMMDD
CRON='50 23 * * 0-4'
DESIRED_STATE=continuous
ARTIFACT_NAME="shadow-worker-${SOURCE_SHA}-${ACTIVATION_ID}"

gh api "repos/${REPO}/actions/runs/${RUN_ID}" \
  --jq '{id,event,status,conclusion,head_sha,head_branch,path,created_at,run_started_at}' \
  >"${audit_dir}/run.json"
ARTIFACT_ID="$(gh api "repos/${REPO}/actions/runs/${RUN_ID}/artifacts" \
  --jq ".artifacts | map(select(.name == \"${ARTIFACT_NAME}\")) | if length == 1 then .[0].id else error(\"artifact not unique\") end")"
gh api "repos/${REPO}/actions/artifacts/${ARTIFACT_ID}" \
  --jq '{id,name,size_in_bytes,expired,digest,workflow_run:{id:.workflow_run.id,head_sha:.workflow_run.head_sha}}' \
  >"${audit_dir}/artifact.json"
gh api -H 'Accept: application/vnd.github+json' \
  "repos/${REPO}/actions/artifacts/${ARTIFACT_ID}/zip" \
  >"${audit_dir}/artifact.zip"

./.venv/bin/python deploy/audit_shadow_schedule_bundle.py \
  --run-json "${audit_dir}/run.json" \
  --artifact-json "${audit_dir}/artifact.json" \
  --artifact-zip "${audit_dir}/artifact.zip" \
  --run-id "${RUN_ID}" --cron "${CRON}" \
  --desired-state "${DESIRED_STATE}" \
  --control-plane-sha "${CONTROL_PLANE_SHA}" \
  --source-sha "${SOURCE_SHA}" --image-digest "${IMAGE_DIGEST}" \
  --activation-id "${ACTIVATION_ID}"
```

For stop, use cron `35 6 * * 1-5` and `DESIRED_STATE=stop`. Only the bounded
PASS summary from this command closes the same-run artifact gate. The start and
stop are still two different GitHub run IDs and must each pass independently
with the same source/image/activation tuple.
