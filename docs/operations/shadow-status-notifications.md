# Protected shadow status notifications

Shadow status notifications run in the protected GitHub Actions control plane,
not inside the Kiwoom application container. The runtime therefore keeps its
existing `slack=false` side-effect contract and cannot import or call the legacy
buy/sell/report notifier.

## Secret boundary

Create one incoming webhook dedicated to a shadow-operations Slack channel.
Store it only as the `production-shadow` environment secret named
`KIWOOM_SHADOW_SLACK_WEBHOOK_URL`.

Do not reuse `CONFIG_JSON`, a bot token, a channel token, a Kiwoom credential,
or a webhook that can post to a trading/execution channel. Do not paste the
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
the dedicated secret before assuming the AWS activation role or sending an SSM
command. `disabled` must be selected explicitly when a no-notification recovery
action is required.

The sender accepts only `https://hooks.slack.com/services/...`, rejects user
information, query strings, fragments, alternate ports, hosts, and redirects,
uses one five-second POST without retry, and requires the exact Slack `200 ok`
acknowledgement.

Messages are fixed summaries built from either accepted runtime evidence or an
allowlisted redacted failure category. They contain the activation ID, a
12-character source prefix, action/status, cycle count when available, and an
explicit statement that account/order/revoke/live trading are disabled. Raw
market data, arbitrary errors, SSM stderr, credentials, and webhook material
cannot enter the message.

Every attempted delivery writes `shadow-status-notification.json` with
`DELIVERED` or `FAILED` and a safe category. Requested Slack delivery failure
fails the workflow, but it runs only after AWS credentials have been cleared and
does not prevent the host-side runtime cleanup path from executing.
