#!/usr/bin/env python3
"""Create or rotate the Kiwoom SecureString pair using hidden input."""

from __future__ import annotations

import argparse
from getpass import getpass
import json
import subprocess
import sys
from typing import Any, cast, Mapping, Protocol, Sequence

try:
    from botocore.exceptions import (  # type: ignore[import-not-found]
        BotoCoreError,
        ClientError,
    )
except ImportError:  # pragma: no cover - boto3 is an operator dependency
    _AWS_ERRORS: tuple[type[BaseException], ...] = ()
else:
    _AWS_ERRORS = (BotoCoreError, ClientError)


DEFAULT_APP_PARAMETER = "/kiwoom-stock/prod/oauth/app-key"
DEFAULT_SECRET_PARAMETER = "/kiwoom-stock/prod/oauth/secret-key"
MAX_VALUE_BYTES = 4096


class ParameterAdminClient(Protocol):
    def describe_parameters(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def put_parameter(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def delete_parameter(self, **kwargs: Any) -> Mapping[str, Any]: ...


class BootstrapError(RuntimeError):
    """A non-sensitive operator error."""


def _validate_value(value: str, label: str) -> str:
    if not value or value != value.strip():
        raise BootstrapError(f"{label} must be a non-empty trimmed value")
    if any(
        ord(character) < 32 or ord(character) == 127
        for character in value
    ):
        raise BootstrapError(f"{label} must be a single printable line")
    if len(value.encode("utf-8")) > MAX_VALUE_BYTES:
        raise BootstrapError(f"{label} exceeds the SecureString size limit")
    return value


def _parameter_exists(client: ParameterAdminClient, name: str) -> bool:
    try:
        response = client.describe_parameters(
            ParameterFilters=[
                {"Key": "Name", "Option": "Equals", "Values": [name]}
            ],
            MaxResults=1,
        )
    except _AWS_ERRORS + (OSError, TypeError, ValueError) as exc:
        raise BootstrapError("parameter metadata lookup failed") from exc
    parameters = response.get("Parameters")
    if not isinstance(parameters, list):
        raise BootstrapError("parameter metadata response is malformed")
    return any(
        isinstance(parameter, Mapping) and parameter.get("Name") == name
        for parameter in parameters
    )


def parameter_states(
    client: ParameterAdminClient,
    *,
    app_parameter: str = DEFAULT_APP_PARAMETER,
    secret_parameter: str = DEFAULT_SECRET_PARAMETER,
) -> tuple[bool, bool]:
    """Return metadata-only existence state without retrieving either value."""

    return (
        _parameter_exists(client, app_parameter),
        _parameter_exists(client, secret_parameter),
    )


def bootstrap_pair(
    client: ParameterAdminClient,
    *,
    app_key: str,
    secret_key: str,
    app_parameter: str = DEFAULT_APP_PARAMETER,
    secret_parameter: str = DEFAULT_SECRET_PARAMETER,
    overwrite: bool = False,
) -> tuple[str, ...]:
    """Write the pair after an existence preflight."""

    if (
        not app_parameter
        or not secret_parameter
        or app_parameter == secret_parameter
    ):
        raise BootstrapError(
            "parameter names must be two distinct non-empty values"
        )
    existing = parameter_states(
        client,
        app_parameter=app_parameter,
        secret_parameter=secret_parameter,
    )
    if overwrite and existing != (True, True):
        raise BootstrapError(
            "rotation requires both parameters to exist before overwrite"
        )
    if not overwrite and existing != (False, False):
        raise BootstrapError(
            "initial creation requires both parameters to be absent"
        )

    values = (
        (app_parameter, _validate_value(app_key, "app key")),
        (secret_parameter, _validate_value(secret_key, "secret key")),
    )
    created: list[str] = []
    try:
        for name, value in values:
            client.put_parameter(
                Name=name,
                Value=value,
                Type="SecureString",
                Tier="Standard",
                Overwrite=overwrite,
                Description="Kiwoom OAuth client credential",
            )
            created.append(name)
    except _AWS_ERRORS + (OSError, TypeError, ValueError) as exc:
        if not overwrite:
            for name in created:
                try:
                    client.delete_parameter(Name=name)
                except _AWS_ERRORS + (OSError, TypeError, ValueError):
                    pass
        raise BootstrapError(
            "SecureString pair write failed; do not restart the application"
        ) from exc
    return (app_parameter, secret_parameter)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--app-parameter", default=DEFAULT_APP_PARAMETER)
    parser.add_argument("--secret-parameter", default=DEFAULT_SECRET_PARAMETER)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument(
        "--check",
        action="store_true",
        help="check parameter metadata only; do not prompt or write",
    )
    return parser


def _create_parameter_client(
    profile: str,
    region: str,
) -> ParameterAdminClient:
    """Use short-lived AWS CLI login credentials for a boto3 client."""

    try:
        completed = subprocess.run(
            [
                "aws",
                "configure",
                "export-credentials",
                "--profile",
                profile,
                "--format",
                "process",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise BootstrapError("AWS CLI credential export failed") from exc
    if completed.returncode != 0:
        raise BootstrapError("AWS CLI login is unavailable or expired")
    try:
        credentials = json.loads(completed.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise BootstrapError("AWS CLI credential export is malformed") from exc
    required = ("AccessKeyId", "SecretAccessKey", "SessionToken")
    if any(
        not isinstance(credentials.get(name), str)
        or not credentials[name]
        for name in required
    ):
        raise BootstrapError("AWS CLI credential export is incomplete")
    try:
        import boto3  # type: ignore[import-not-found]

        session = boto3.Session(
            aws_access_key_id=credentials["AccessKeyId"],
            aws_secret_access_key=credentials["SecretAccessKey"],
            aws_session_token=credentials["SessionToken"],
            region_name=region,
        )
        return cast(ParameterAdminClient, session.client("ssm"))
    except _AWS_ERRORS + (ImportError, TypeError, ValueError) as exc:
        raise BootstrapError("AWS SSM client creation failed") from exc


def main(
    argv: Sequence[str] | None = None,
    *,
    client: ParameterAdminClient | None = None,
) -> int:
    args = _parser().parse_args(argv)
    try:
        if client is None:
            client = _create_parameter_client(args.profile, args.region)
        states = parameter_states(
            client,
            app_parameter=args.app_parameter,
            secret_parameter=args.secret_parameter,
        )
        if args.check:
            print(
                "parameter metadata check: "
                f"app={'exists' if states[0] else 'missing'}, "
                f"secret={'exists' if states[1] else 'missing'}"
            )
            return 0
        if args.overwrite and states != (True, True):
            raise BootstrapError(
                "rotation requires both parameters to exist"
            )
        if not args.overwrite and states != (False, False):
            raise BootstrapError(
                "initial creation requires both parameters to be absent"
            )
        app_key = getpass("Kiwoom App Key (hidden): ")
        secret_key = getpass("Kiwoom Secret Key (hidden): ")
        bootstrap_pair(
            client,
            app_key=app_key,
            secret_key=secret_key,
            app_parameter=args.app_parameter,
            secret_parameter=args.secret_parameter,
            overwrite=args.overwrite,
        )
    except _AWS_ERRORS + (
        BootstrapError,
        ImportError,
        OSError,
        TypeError,
        ValueError,
    ) as exc:
        if isinstance(exc, BootstrapError):
            message = str(exc)
        else:
            message = "AWS credential bootstrap failed"
        print(f"kiwoom parameter bootstrap failed: {message}", file=sys.stderr)
        return 1
    print("Kiwoom SecureString pair stored; values were not printed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
