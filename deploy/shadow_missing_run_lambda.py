#!/usr/bin/env python3
"""Read-only EventBridge/Lambda adapter for the Shadow missing-run classifier.

The adapter owns only observation, bounded alert deduplication, and metrics. It
does not dispatch, rerun, stop, start, call SSM/EC2, or place broker orders.
The production handler uses the public GitHub Runs API by default; the CLI is a
fixture-only dry-run path and never makes a network request.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import ssl
from typing import Any, Callable, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    Request,
    build_opener,
)

try:
    from .notify_shadow_status import (
        SlackStatusError,
        deliver,
        validate_webhook,
    )
    from .shadow_missing_run_detector import (
        ACTIVATION_WORKFLOW_ID,
        AUDIT_WORKFLOW_ID,
        CONTRACTS,
        KST,
        MAX_RUNS,
        MissingRunError,
        RunQueryResult,
        classify_occurrence,
    )
except ImportError:
    from notify_shadow_status import (  # type: ignore[no-redef]
        SlackStatusError,
        deliver,
        validate_webhook,
    )
    from shadow_missing_run_detector import (  # type: ignore[no-redef]
        ACTIVATION_WORKFLOW_ID,
        AUDIT_WORKFLOW_ID,
        CONTRACTS,
        KST,
        MAX_RUNS,
        MissingRunError,
        RunQueryResult,
        classify_occurrence,
    )


REPOSITORY = "SpiceChicken/kiwoom_stock"
RUNS_API = (
    f"https://api.github.com/repos/{REPOSITORY}/actions/"
    "workflows/{workflow_id}/runs"
)
MAX_RESPONSE_BYTES = 512 * 1024
MAX_FIXTURE_BYTES = 512 * 1024
HTTP_TIMEOUT_SECONDS = 5.0
ALERT_TTL_SECONDS = 35 * 24 * 60 * 60
HEARTBEAT_TTL_SECONDS = 35 * 24 * 60 * 60
ALERT_STATUSES = frozenset({
    "DELAYED_OR_MISSING",
    "MISSING_ACTIVATION",
    "IN_PROGRESS",
    "STUCK_ACTIVATION",
    "ACTIVATION_FAILED",
    "DUPLICATE_ACTIVATION",
    "DUPLICATE_AUDIT",
    "MISSING_AUDIT",
    "AUDIT_IN_PROGRESS",
    "AUDIT_FAILED",
    "API_VISIBILITY_FAILURE",
})
RUN_KEYS = {
    "id", "event", "status", "conclusion", "head_branch", "head_sha",
    "path", "created_at", "run_started_at", "updated_at",
}
SCHEDULES = frozenset(CONTRACTS)
PHASES = frozenset({"presence", "closure"})
UTC_TIMESTAMP_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:"
    r"[0-9]{2}:[0-9]{2}Z"
)


class DetectorAdapterError(ValueError):
    """A bounded adapter or external visibility failure."""


class ResponsePort(Protocol):
    status: int

    def read(self, amount: int = -1) -> bytes: ...

    def __enter__(self) -> "ResponsePort": ...

    def __exit__(self, *args: object) -> None: ...


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        return None


class QueryPort(Protocol):
    def __call__(self, workflow_id: int, event: str) -> RunQueryResult: ...


class ClaimStore(Protocol):
    def claim(self, key: str, expires_at: int) -> bool: ...

    def heartbeat(self, key: str, expires_at: int) -> None: ...


class AlertSink(Protocol):
    def send(self, record: Mapping[str, object]) -> None: ...


class MetricSink(Protocol):
    def emit(self, record: Mapping[str, object]) -> None: ...


def _reject_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise DetectorAdapterError("json_duplicate_key")
        value[key] = item
    return value


def _reject_non_json_constant(_value: str) -> object:
    raise DetectorAdapterError("json_constant_invalid")


def _load_json_bytes(raw: bytes, *, maximum: int) -> object:
    if len(raw) > maximum:
        raise DetectorAdapterError("json_oversized")
    try:
        return json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_json_constant,
        )
    except DetectorAdapterError:
        raise
    except (json.JSONDecodeError, UnicodeError, TypeError, ValueError):
        raise DetectorAdapterError("json_invalid") from None


def _parse_utc(value: object) -> datetime:
    if not isinstance(value, str) or UTC_TIMESTAMP_RE.fullmatch(value) is None:
        raise DetectorAdapterError("timestamp_invalid")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise DetectorAdapterError("timestamp_invalid") from None
    return parsed.replace(tzinfo=timezone.utc)


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate_request(schedule: object, phase: object) -> tuple[str, str]:
    if (
        not isinstance(schedule, str)
        or schedule not in SCHEDULES
        or not isinstance(phase, str)
        or phase not in PHASES
    ):
        raise DetectorAdapterError("request_invalid")
    return schedule, phase


def _project_runs(value: object, *, workflow_id: int) -> RunQueryResult:
    if not isinstance(value, Mapping):
        raise DetectorAdapterError("api_shape_invalid")
    if set(value) != {"total_count", "workflow_runs"}:
        raise DetectorAdapterError("api_shape_invalid")
    total_count = value.get("total_count")
    runs = value.get("workflow_runs")
    if type(total_count) is not int or total_count < 0:
        raise DetectorAdapterError("api_shape_invalid")
    if not isinstance(runs, list) or len(runs) > MAX_RUNS:
        raise DetectorAdapterError("api_shape_invalid")
    projected: list[Mapping[str, object]] = []
    for run in runs:
        if not isinstance(run, Mapping):
            raise DetectorAdapterError("api_shape_invalid")
        if any(key not in run for key in RUN_KEYS):
            raise DetectorAdapterError("api_projection_invalid")
        projected.append({key: run[key] for key in RUN_KEYS})
    try:
        return RunQueryResult.success(workflow_id, projected)
    except MissingRunError as error:
        raise DetectorAdapterError("api_projection_invalid") from error


def query_workflow_runs(
    workflow_id: int,
    event: str,
    *,
    opener: Callable[..., ResponsePort] | None = None,
) -> RunQueryResult:
    """Fetch exactly one bounded workflow-runs projection from GitHub."""

    if type(workflow_id) is not int or workflow_id <= 0:
        raise DetectorAdapterError("request_invalid")
    if event not in {"schedule", "workflow_run"}:
        raise DetectorAdapterError("request_invalid")
    query = urlencode({
        "branch": "main",
        "event": event,
        "per_page": str(MAX_RUNS),
    })
    url = f"{RUNS_API.format(workflow_id=workflow_id)}?{query}"
    request = Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "kiwoom-shadow-missing-run-detector/1",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method="GET",
    )
    if opener is None:
        try:
            context = ssl.create_default_context()
            opener = build_opener(
                NoRedirect(), HTTPSHandler(context=context),
            ).open
        except (OSError, ssl.SSLError):
            raise DetectorAdapterError("api_visibility_failure") from None
    try:
        with opener(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            if response.status != 200:
                raise DetectorAdapterError("api_visibility_failure")
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except DetectorAdapterError:
        raise
    except HTTPError:
        raise DetectorAdapterError("api_visibility_failure") from None
    except (OSError, URLError, TimeoutError, ssl.SSLError):
        raise DetectorAdapterError("api_visibility_failure") from None
    try:
        decoded = _load_json_bytes(body, maximum=MAX_RESPONSE_BYTES)
        return _project_runs(decoded, workflow_id=workflow_id)
    except DetectorAdapterError as error:
        if error.args and error.args[0] == "api_visibility_failure":
            raise
        raise DetectorAdapterError("api_visibility_failure") from error


def _occurrence_metadata(
    schedule: str, phase: str, checked_at: datetime,
) -> dict[str, object]:
    checked_utc = checked_at.astimezone(timezone.utc)
    local = checked_utc.astimezone(KST)
    contract = CONTRACTS[schedule]
    expected = local.replace(
        hour=contract.hour, minute=contract.minute, second=0, microsecond=0,
    ).astimezone(timezone.utc)
    return {
        "occurrence_id": (
            expected.astimezone(KST).strftime("%Y-%m-%d#") + contract.label
        ),
        "desired_state": contract.desired_state,
        "phase": phase,
        "expected_at_utc": _format_utc(expected),
        "checked_at_utc": _format_utc(checked_utc),
    }


def _base_record(
    *, schedule: str, phase: str, checked_at: datetime, status: str,
) -> dict[str, object]:
    metadata = _occurrence_metadata(schedule, phase, checked_at)
    return {
        "detector_schema_version": 1,
        "schema_version": 1,
        "schedule": schedule,
        **metadata,
        "status": status,
        "activation_count": 0,
        "audit_count": 0,
        "activation_run_id": None,
        "audit_run_id": None,
    }


def evaluate(
    event: Mapping[str, object],
    *,
    query: QueryPort,
    checked_at: datetime,
) -> dict[str, object]:
    """Evaluate one detector event without alert or recovery side effects."""

    if (
        not isinstance(event, Mapping)
        or set(event) - {"schedule", "phase", "checked_at_utc"}
    ):
        raise DetectorAdapterError("request_invalid")
    schedule, phase = _validate_request(
        event.get("schedule"), event.get("phase"),
    )
    if checked_at.tzinfo is None or checked_at.utcoffset() is None:
        raise DetectorAdapterError("timestamp_invalid")
    checked_utc = checked_at.astimezone(timezone.utc)
    activation_event = "schedule"
    audit_event = "workflow_run"
    try:
        activation_query = query(ACTIVATION_WORKFLOW_ID, activation_event)
        audit_query: RunQueryResult | None = None
        if phase == "closure":
            audit_query = query(AUDIT_WORKFLOW_ID, audit_event)
        result = classify_occurrence(
            schedule=schedule,
            phase=phase,
            checked_at=checked_utc,
            activation_query=activation_query,
            audit_query=audit_query,
        )
    except DetectorAdapterError:
        return _base_record(
            schedule=schedule, phase=phase, checked_at=checked_utc,
            status="API_VISIBILITY_FAILURE",
        )
    except MissingRunError as error:
        category = str(error)
        if category == "api_visibility_failure":
            return _base_record(
                schedule=schedule, phase=phase, checked_at=checked_utc,
                status="API_VISIBILITY_FAILURE",
            )
        raise DetectorAdapterError("classifier_contract_failure") from error
    return {
        "detector_schema_version": 1,
        "schedule": schedule,
        **result,
    }


def _event_checked_at(
    event: Mapping[str, object], *, now: Callable[[], datetime],
) -> datetime:
    value = event.get("checked_at_utc")
    if value is None:
        result = now()
        if result.tzinfo is None or result.utcoffset() is None:
            raise DetectorAdapterError("timestamp_invalid")
        return result.astimezone(timezone.utc)
    return _parse_utc(value)


def _alert_message(record: Mapping[str, object]) -> str:
    status = record.get("status")
    occurrence = record.get("occurrence_id")
    schedule = record.get("schedule")
    phase = record.get("phase")
    if not all(
        isinstance(item, str)
        for item in (status, occurrence, schedule, phase)
    ):
        raise DetectorAdapterError("record_invalid")
    return (
        "[KIWOOM SHADOW DETECTOR] "
        f"{status} | occurrence={occurrence} | schedule={schedule} | "
        f"phase={phase} | live-trading=disabled"
    )


@dataclass
class MemoryClaimStore:
    """Deterministic fixture store; never used by the production handler."""

    claims: set[str]
    heartbeats: dict[str, int]

    def claim(self, key: str, expires_at: int) -> bool:
        del expires_at
        if key in self.claims:
            return False
        self.claims.add(key)
        return True

    def heartbeat(self, key: str, expires_at: int) -> None:
        self.heartbeats[key] = expires_at


class DynamoClaimStore:
    """Minimal conditional-claim store backed by one DynamoDB table."""

    def __init__(self, table: Any) -> None:
        self._table = table

    def claim(self, key: str, expires_at: int) -> bool:
        try:
            self._table.put_item(
                Item={"pk": key, "expires_at": expires_at},
                ConditionExpression="attribute_not_exists(pk)",
            )
            return True
        except Exception as error:  # boto3 is optional in local/test envs.
            if error.__class__.__name__ == "ConditionalCheckFailedException":
                return False
            raise DetectorAdapterError("dedupe_store_failure") from error

    def heartbeat(self, key: str, expires_at: int) -> None:
        try:
            self._table.put_item(Item={"pk": key, "expires_at": expires_at})
        except Exception as error:  # boto3 is optional in local/test envs.
            raise DetectorAdapterError("dedupe_store_failure") from error


class SlackAlertSink:
    def __init__(self, webhook: str) -> None:
        try:
            self._webhook = validate_webhook(webhook)
        except SlackStatusError as error:
            raise DetectorAdapterError("alert_webhook_invalid") from error

    def send(self, record: Mapping[str, object]) -> None:
        try:
            deliver(self._webhook, _alert_message(record))
        except SlackStatusError as error:
            raise DetectorAdapterError("alert_delivery_failure") from error


class CloudWatchMetricSink:
    def __init__(self, client: Any) -> None:
        self._client = client

    def emit(self, record: Mapping[str, object]) -> None:
        status = record.get("status")
        schedule = record.get("schedule")
        phase = record.get("phase")
        if not all(
            isinstance(item, str) for item in (status, schedule, phase)
        ):
            raise DetectorAdapterError("record_invalid")
        self._client.put_metric_data(
            Namespace="Kiwoom/Shadow",
            MetricData=[
                {
                    "MetricName": "DetectorStatus",
                    "Value": 1,
                    "Unit": "Count",
                    "Dimensions": [
                        {"Name": "Schedule", "Value": schedule},
                        {"Name": "Phase", "Value": phase},
                        {"Name": "Status", "Value": status},
                    ],
                },
                {
                    "MetricName": "Heartbeat",
                    "Value": 1,
                    "Unit": "Count",
                    "Dimensions": [
                        {"Name": "Schedule", "Value": schedule},
                        {"Name": "Phase", "Value": phase},
                    ],
                },
            ],
        )


def process(
    event: Mapping[str, object],
    *,
    query: QueryPort,
    store: ClaimStore,
    sink: AlertSink | None = None,
    metrics: MetricSink | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    alert_enabled: bool = True,
) -> dict[str, object]:
    checked_at = _event_checked_at(event, now=now)
    record = evaluate(event, query=query, checked_at=checked_at)
    occurrence = record.get("occurrence_id")
    phase = record.get("phase")
    status = record.get("status")
    if not all(isinstance(item, str) for item in (occurrence, phase, status)):
        raise DetectorAdapterError("record_invalid")
    expires_at = int(checked_at.timestamp()) + HEARTBEAT_TTL_SECONDS
    store.heartbeat(f"heartbeat#{occurrence}#{phase}", expires_at)
    claimed = False
    delivered = False
    if status in ALERT_STATUSES and alert_enabled:
        alert_key = f"alert#{occurrence}#{phase}#{status}"
        claimed = store.claim(
            alert_key, int(checked_at.timestamp()) + ALERT_TTL_SECONDS,
        )
        if claimed and sink is not None:
            sink.send(record)
            delivered = True
    if metrics is not None:
        metrics.emit(record)
    return {
        **record,
        "alert_claimed": claimed,
        "alert_delivered": delivered,
    }


def _secret_webhook(client: Any, secret_arn: str) -> str:
    try:
        response = client.get_secret_value(SecretId=secret_arn)
        value = response.get("SecretString")
    except Exception as error:
        raise DetectorAdapterError("alert_secret_failure") from error
    if not isinstance(value, str) or not value:
        raise DetectorAdapterError("alert_secret_failure")
    try:
        decoded = _load_json_bytes(value.encode("utf-8"), maximum=4096)
    except DetectorAdapterError:
        decoded = value
    if isinstance(decoded, Mapping):
        if set(decoded) != {"webhook_url"}:
            raise DetectorAdapterError("alert_secret_failure")
        decoded = decoded.get("webhook_url")
    if not isinstance(decoded, str):
        raise DetectorAdapterError("alert_secret_failure")
    return decoded


def _boto3_clients() -> tuple[Any, Any, Any]:
    try:
        import boto3
        resource = boto3.resource("dynamodb")
        secrets = boto3.client("secretsmanager")
        cloudwatch = boto3.client("cloudwatch")
    except Exception as error:
        raise DetectorAdapterError("aws_client_failure") from error
    return resource, secrets, cloudwatch


def handler(event: object, context: object) -> dict[str, object]:
    """Lambda entry point; production configuration is environment-bound."""

    del context
    if not isinstance(event, Mapping):
        raise DetectorAdapterError("request_invalid")
    if "checked_at_utc" in event:
        raise DetectorAdapterError("request_invalid")
    table_name = os.environ.get("SHADOW_DETECTOR_TABLE_NAME")
    if not table_name:
        raise DetectorAdapterError("dedupe_table_not_configured")
    mode = os.environ.get("SHADOW_DETECTOR_ALERT_MODE", "metrics-only")
    if mode not in {"metrics-only", "slack"}:
        raise DetectorAdapterError("alert_mode_invalid")
    resource, secrets, cloudwatch = _boto3_clients()
    store = DynamoClaimStore(resource.Table(table_name))
    sink: SlackAlertSink | None = None
    if mode == "slack":
        secret_arn = os.environ.get("SHADOW_DETECTOR_SLACK_SECRET_ARN")
        if not secret_arn:
            raise DetectorAdapterError("alert_secret_not_configured")
        sink = SlackAlertSink(_secret_webhook(secrets, secret_arn))
    result = process(
        event,
        query=query_workflow_runs,
        store=store,
        sink=sink,
        metrics=CloudWatchMetricSink(cloudwatch),
        alert_enabled=mode == "slack",
    )
    return result


def _load_fixture(path: Path) -> Mapping[str, object]:
    try:
        raw = path.read_bytes()
    except OSError:
        raise DetectorAdapterError("fixture_invalid") from None
    value = _load_json_bytes(raw, maximum=MAX_FIXTURE_BYTES)
    if not isinstance(value, Mapping):
        raise DetectorAdapterError("fixture_invalid")
    if set(value) not in (
        {"activation_runs"}, {"activation_runs", "audit_runs"}
    ):
        raise DetectorAdapterError("fixture_invalid")
    return value


def _fixture_query(fixture: Mapping[str, object]) -> QueryPort:
    def query(workflow_id: int, event: str) -> RunQueryResult:
        if workflow_id == ACTIVATION_WORKFLOW_ID and event == "schedule":
            runs = fixture.get("activation_runs")
        elif workflow_id == AUDIT_WORKFLOW_ID and event == "workflow_run":
            runs = fixture.get("audit_runs", [])
        else:
            raise DetectorAdapterError("fixture_query_invalid")
        if not isinstance(runs, list):
            raise DetectorAdapterError("fixture_invalid")
        try:
            return RunQueryResult.success(workflow_id, runs)
        except MissingRunError as error:
            raise DetectorAdapterError("fixture_invalid") from error

    return query


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--schedule", choices=sorted(SCHEDULES), required=True)
    parser.add_argument("--phase", choices=sorted(PHASES), required=True)
    parser.add_argument("--checked-at-utc", required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        checked_at = _parse_utc(args.checked_at_utc)
        fixture = _load_fixture(args.fixture)
        record = process(
            {
                "schedule": args.schedule,
                "phase": args.phase,
                "checked_at_utc": args.checked_at_utc,
            },
            query=_fixture_query(fixture),
            store=MemoryClaimStore(set(), {}),
            alert_enabled=False,
            now=lambda: checked_at,
        )
        print(json.dumps(record, sort_keys=True, separators=(",", ":")))
    except (DetectorAdapterError, MissingRunError) as error:
        print(f"shadow missing-run dry-run failed: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
