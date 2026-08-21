# Protected shadow status notifications

Shadow status notifications run in the protected GitHub Actions control plane,
not inside the Kiwoom application container. The runtime therefore keeps its
existing `slack=false` side-effect contract and cannot import or call the legacy
buy/sell/report notifier.

The existing Slack operating information is reused only through the protected
`production-shadow` notification path: prefer
`KIWOOM_SHADOW_SLACK_WEBHOOK_URL`, with the documented `CONFIG_JSON.webhook_url`
compatibility fallback until that migration is closed. This does not mean that
all historical application Slack/report delivery is restored. A delivery claim
requires the workflow's redacted `DELIVERED` evidence. See
[current-state.md](current-state.md) for the current no-live-trading status.

## Secret boundary

Create one incoming webhook dedicated to a shadow-operations Slack channel.
Store it as the `production-shadow` environment secret named
`KIWOOM_SHADOW_SLACK_WEBHOOK_URL`. While that dedicated secret is unset or
empty, the workflow may use the top-level `webhook_url` in repository secret
`CONFIG_JSON` as a compatibility fallback.

Do not use a bot token, a channel token, a Kiwoom credential, or a webhook that
can post to a trading/execution channel. Do not paste the
webhook into chat, a command argument, a workflow input, or a repository file.

An operator can set the secret through the GitHub environment UI. With an
authenticated `gh` session, the following command prompts on standard input and
does not require putting the value in shell history:

```bash
gh secret set KIWOOM_SHADOW_SLACK_WEBHOOK_URL \
  --env production-shadow \
  --repo SpiceChicken/kiwoom_stock
```

Confirm only the secret name, never its value:

```bash
gh api repos/SpiceChicken/kiwoom_stock/environments/production-shadow/secrets \
  --jq '.secrets[].name'
```

## Delivery contract

The activation workflow defaults `status_notification` to `slack`. It validates
the dedicated secret, or the compatibility fallback only when dedicated is
empty, before assuming the AWS activation role or sending an SSM command.
Non-empty invalid dedicated values fail closed and never fall back.
`CONFIG_JSON` is scoped to the two Slack steps and is never sent to AWS, SSM,
or runtime.
`disabled` must be selected explicitly when a no-notification recovery action
is required.

The sender accepts only `https://hooks.slack.com/services/...`, rejects C0/DEL
control characters, user information, query strings, fragments, alternate
ports, hosts, and redirects, uses one five-second POST without retry, and
requires the exact Slack `200 ok` acknowledgement.

Messages are fixed summaries built from either accepted runtime evidence or an
allowlisted redacted failure category. They contain the activation ID, a
12-character source prefix, action/status, cycle count when available, and an
explicit statement that account/order/revoke/live trading are disabled. Raw
market data, arbitrary errors, SSM stderr, credentials, raw `CONFIG_JSON`, and
webhook material cannot enter the message.

`PhysicalStateValidationError` is reported only when the bounded invocation
diagnostic accepts the exact allowlisted sentinel or the same error type in a
validated terminal for the immutable activation tuple. Its exception body is
never copied; the safe category is `physical_state_validation_error`. In the
diagnostic terminal summary, only the exact allowlisted
`PhysicalStateValidationError` value is preserved. Every other validated
terminal `error_type` is reduced to `null` rather than copied into the durable
artifact.

Only the worker's exact complete line
`shadow worker failed: shadow container is absent; stop identity cannot be proven`
is action-specific. For `stop`, it is emitted as `stop_target_absent` and the
fixed Slack prefix is
`STOP TARGET ABSENT`. This means only that there was no target whose identity
could be proven for cleanup. It does not claim that start failed, cleanup had
already completed, or the tuple drifted. The bare phrase and prefixed/suffixed
near matches are rejected. The sender temporarily accepts legacy
`container_absent` diagnostics for stop with the same cause-neutral wording.

For a scheduled run, a valid strict `shadow-schedule-observation.json` adds only
`schedule_delay=<seconds>s` to the end of the fixed message. The notifier
revalidates the artifact against the current expected GitHub run ID and event
cron; either mismatch is `invalid`. Manual dispatch does not add a schedule
suffix. A missing or invalid scheduled observation does not invent `0s` or
expose the API response; it leaves the message unchanged and records
`schedule_observation=invalid` in the notification receipt. Manual receipts
record `schedule_observation=n-a`.

Every attempted delivery writes `shadow-status-notification.json` with
`DELIVERED` or `FAILED`, a safe category, and the bounded schedule-observation
state. This eight-key receipt is exact schema version 2. Schema version 1 is the
legacy seven-key shape without `schedule_observation`; producers do not add the
new key while claiming v1, and strict consumers must select the key set by
`schema_version`. Requested Slack delivery failure fails the workflow, but it
runs only after AWS credentials have been cleared and does not prevent the
host-side runtime cleanup path from executing.

Once `KIWOOM_SHADOW_SLACK_WEBHOOK_URL` is provisioned and verified, remove the
`CONFIG_JSON` fallback and its two-step wiring as the forward migration. To
roll back safely, restore only that compatibility wiring; no runtime or AWS
secret path is involved.
