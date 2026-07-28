# Kiwoom credential and access-token lifecycle runbook

## Current activation boundary

The repository remains `DISARMED`. Do not place a real App Key, Secret Key, or
access token in chat, Git, `.env`, process environment, command arguments,
container images, CI, logs, reports, or test fixtures. This runbook does not
authorize a real Kiwoom request, order, deployment, or production activation.

Mock and production credentials must have different owners, files, access
policies, and allowed-IP registrations. The application accepts only the typed
origins derived from `KIWOOM_API_MODE`:

- mock: `https://mockapi.kiwoom.com`
- prod: `https://api.kiwoom.com`

Free-form base URLs, redirects, ambient HTTP proxies, and disabled TLS
verification are not supported.

## Token ownership

One `Authenticator` owns one in-memory token lease. Token values use a
redacted wrapper and are not persisted. Python memory encryption or complete
zeroization is not claimed.

`expires_dt` must contain exactly 14 ASCII digits in `%Y%m%d%H%M%S`. Kiwoom
does not currently document its timezone. Until an approved mock/staging
validation confirms the meaning, the temporary implementation policy is:

1. interpret `expires_dt` as `Asia/Seoul`;
2. convert it immediately to aware UTC;
3. refresh at `expires_at - min(5 minutes, initial TTL × 10%)`.

Missing, malformed, non-future expiry, nonzero/missing `return_code`, empty or
unsafe token text, and a token type other than Bearer all fail closed.
`expires_in` is not used.

Issuance retries only HTTP 429, with three total attempts. Timeout, connection
failure, 5xx, redirect, and invalid JSON are typed failures and do not retain
or log raw request/response/exception objects.

Concurrent callers share both a successful issuance and one safe failure
snapshot. A failure is cached for one second so already-waiting callers do not
serially repeat the same request; a later caller may start one new bounded
attempt after that window.

## Read transport and 401 behavior

Kiwoom read services explicitly mark requests `read_only=True`. Only those
requests may use bounded timeout/connection/429/5xx retry. Unknown or order
semantics are sent once and are never replayed by transport policy.

An explicit read-only 401 may refresh only when the request's rejected token
generation is still current. If a peer already published a newer generation,
the stale 401 reuses it instead of invalidating it. Each request replays at most
once; a second 401 is terminal. Unknown/order semantics receive no 401 replay.

Enabled runtime startup calls explicit auth readiness once before engine
construction. A readiness failure rolls back the local client and persistence
resources and never enters the engine loop. Runtime client close occurs after
post-market or crash reporting consumers on every terminal path. Minute-chart
clients close on empty, success, and error paths. These are local-only closes;
none automatically call broker revoke.

## Explicit token revocation

Normal object construction and process shutdown do not call the real revoke
endpoint. Revocation is an explicit operator/rotation action:

1. keep execution `DISARMED`;
2. reconcile working orders, positions, and local ledger state;
3. invoke the current owner's explicit `revoke()` exactly once;
4. record only one result: `REVOKED`, `REJECTED`, or `UNKNOWN`;
5. never retry `UNKNOWN` automatically;
6. create a new credential/token owner only after operator reconciliation.

`revoke()` sends `au10002` to `/oauth2/revoke`, clears the local lease, closes
the owner for all future issuance, and returns:

- `REVOKED`: HTTP 200, valid JSON object, integer `return_code == 0`;
- `REJECTED`: HTTP 200, valid JSON object, integer nonzero `return_code`;
- `UNKNOWN`: no local lease, timeout/transport failure, non-200 response,
  invalid JSON, or ambiguous return contract.

All three results retire the local owner. `UNKNOWN` must not be recorded as a
successful broker-side revocation.

## Rotation and incident response handoff

For suspected exposure, keep execution `DISARMED`, prevent automatic restart,
and explicitly revoke the current access token once. Preserve only its typed
`REVOKED`, `REJECTED`, or `UNKNOWN` result; retire the local token owner in all
three cases and do not automatically retry `UNKNOWN`.

Static App Key and Secret rotation, provider-version removal, incident
investigation, and history/log/artifact cleanup are owned exclusively by the
[credential rotation and incident runbook](../operations/credential-rotation.md).
That runbook also defines the separate approvals required before external
changes or re-arming.

## Logging boundary

Product logging uses allowlisted metadata first: category, HTTP status,
attempt, API ID, and typed result. Configured handlers also receive a
second-layer redaction filter for registered credential/token sentinels and
Bearer values, including nested messages and formatted exceptions.

This is defense in depth, not universal redaction. Arbitrary third-party
handlers, direct writes, wire-debug logging, values transformed before
registration, and process-memory inspection are outside that guarantee.

## Activation blockers

Before any real mock/prod activation, validator evidence is still required for:

- actual Kiwoom `expires_dt` timezone and `au10002` semantics;
- authorization-header behavior from the approved mock environment;
- Docker UID 10001 secret-file owner/mode/readability on the deployment target;
- outbound proxy/DNS/network policy and exact-origin TLS observation;
- no-order issuance, refresh, one-shot revoke, restart, and negative log scan.

No real credential or network call was used to create or test this runbook.
