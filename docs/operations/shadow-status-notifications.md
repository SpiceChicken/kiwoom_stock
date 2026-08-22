# Protected shadow status notifications

Shadow status notifications run outside the Kiwoom application container through
the protected C* observer boundary. The runtime therefore keeps its existing
`slack=false` side-effect contract and cannot import or call the legacy
buy/sell/report notifier.

The existing Slack operating information is reusable only through the protected
observer notification path: prefer
`KIWOOM_SHADOW_SLACK_WEBHOOK_URL`, with the documented `CONFIG_JSON.webhook_url`
compatibility fallback until that migration is closed. This does not mean that
all historical application Slack/report delivery is restored. The active C*
cutover is `metrics-only` and has no registered webhook secret, so Slack delivery
is currently disabled. If a later explicitly approved deployment enables it, a
delivery claim requires redacted `DELIVERED` evidence. See
[current-state.md](current-state.md) for the current no-live-trading status.

## Secret boundary

If Slack is explicitly approved later, create one incoming webhook dedicated to
a shadow-operations Slack channel and store it in the pre-created AWS Secrets
Manager secret permitted to the C* observer. The current `metrics-only` cutover
does not create or read this secret. The historical GitHub environment secret
path is not an active delivery path.

Do not use a bot token, a channel token, a Kiwoom credential, or a webhook that
can post to a trading/execution channel. Do not paste the
webhook into chat, a command argument, a workflow input, or a repository file.

Provisioning and verifying that secret is a separate AWS change. Never record
the value, ARN, or a delivery response containing the webhook in Git, chat,
command arguments, workflow inputs, or this repository.

## Delivery contract

The C* IaC parameter `AlertMode` defaults to `metrics-only`. An explicitly
approved `slack` deployment must validate the observer's dedicated secret before
delivery. Invalid or missing secret metadata fails closed. Legacy `CONFIG_JSON`
compatibility values are not an active C* input and are never sent to AWS, SSM,
or runtime.
`metrics-only` remains the required setting for no-notification recovery.

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
near matches are rejected. The retired legacy `container_absent` category is
not accepted; current producer and consumer both use the action-specific
`stop_target_absent` contract.

Evidence and diagnostic JSON are bounded and parsed strictly. Duplicate keys,
non-standard numeric constants such as `NaN`, and booleans used in integer
fields are rejected instead of being interpreted as a valid status artifact.

For a current C* occurrence, the observer binds any notification receipt to the
cloud occurrence/session identity and the immutable release tuple. Historical
GitHub run IDs and cron observations are not accepted as current evidence.

Every attempted delivery writes `shadow-status-notification.json` with
`DELIVERED` or `FAILED`, a safe category, and the bounded C* occurrence/session
identity. Legacy schema versions remain read-only compatibility material.
Notification failure marks evidence closure as failed/alerted but does not
prevent the host-side runtime cleanup path from executing.

Do not enable Slack by changing this document. Use a separately reviewed C* IaC
parameter, pre-created secret, least-privilege observer permission, and
read-back. The current no-notification state remains `metrics-only`.
