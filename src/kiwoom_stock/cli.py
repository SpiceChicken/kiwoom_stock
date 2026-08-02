"""No-side-effect package command entry points."""

import argparse
import json
import sys
from typing import Optional, Sequence

from kiwoom_stock import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kiwoom_stock",
        description="Kiwoom stock monitoring package utilities.",
    )
    parser.add_argument("--version", action="version", version="kiwoom_stock %s" % __version__)
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="validate environment settings without starting external clients or trading",
    )
    subparsers = parser.add_subparsers(dest="command")
    shadow_once = subparsers.add_parser(
        "shadow-once",
        help="run one fixed-target, market-only shadow calculation cycle",
    )
    shadow_once.add_argument("--source-sha", required=True)
    shadow_once.add_argument("--image-digest", required=True)
    shadow_once.add_argument("--activation-id", required=True)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.check_config and args.command is not None:
        parser.error("--check-config cannot be combined with a command")
    if args.check_config:
        from kiwoom_stock.application.credential_preflight import (
            preflight_environment,
        )
        from kiwoom_stock.application.credentials import (
            CredentialProviderError,
        )
        from kiwoom_stock.core import config
        from kiwoom_stock.infrastructure.kiwoom_credentials import (
            StrictFileCredentialProvider,
            credential_repository_boundary,
        )
        from kiwoom_stock.settings import SettingsValidationError

        try:
            preflight = preflight_environment(
                config,
                lambda credentials_dir: StrictFileCredentialProvider(
                    credentials_dir,
                    repository_root=credential_repository_boundary(),
                ),
            )
            settings = preflight.settings
        except (CredentialProviderError, SettingsValidationError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        for warning in settings.diagnostics.warnings:
            print("warning: %s" % warning, file=sys.stderr)
        print("Configuration OK")
        return 0
    if args.command == "shadow-once":
        from kiwoom_stock.application.execution import (
            ActivationTuple,
            ExecutionPolicy,
            SHADOW_PROCESS_LOCK_PATH,
        )
        from kiwoom_stock.application.runtime import create_shadow_runtime
        from kiwoom_stock.application.shadow_worker import run_shadow_once_managed
        from kiwoom_stock.core import config
        from kiwoom_stock.infrastructure.shadow_process_lock import ShadowProcessLock
        from kiwoom_stock.settings import SettingsValidationError

        try:
            settings = config.validate_environment_settings()
            policy = ExecutionPolicy.for_request(
                settings.execution.mode,
                ActivationTuple(
                    source_sha=args.source_sha,
                    image_digest=args.image_digest,
                    activation_id=args.activation_id,
                ),
            )
            result = run_shadow_once_managed(
                policy,
                lock_path=SHADOW_PROCESS_LOCK_PATH,
                runtime_factory=lambda admitted, admission: create_shadow_runtime(
                    policy=admitted,
                    settings=settings,
                    admission=admission,
                ),
                lock_factory=ShadowProcessLock,
            )
        except SettingsValidationError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        except BaseException as exc:
            print(
                json.dumps(
                    {"status": "FAILED", "error_type": type(exc).__name__},
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
            return 1
        print(json.dumps(result.to_safe_dict(), sort_keys=True))
        return 0
    parser.print_help()
    return 0
