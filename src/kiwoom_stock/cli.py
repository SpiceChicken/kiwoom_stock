"""No-side-effect package command entry points."""

import argparse
import hashlib
import json
import os
from pathlib import Path
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
        help=(
            "run one fixed-target market-to-isolated-paper-ledger E2E cycle "
            "with broker orders disabled"
        ),
    )
    shadow_once.add_argument("--source-sha", required=True)
    shadow_once.add_argument("--image-digest", required=True)
    shadow_once.add_argument("--activation-id", required=True)
    shadow_worker = subparsers.add_parser(
        "shadow-worker",
        help=(
            "run the bounded fixed-target continuous paper-ledger E2E worker "
            "with broker orders disabled"
        ),
    )
    shadow_worker.add_argument("--source-sha", required=True)
    shadow_worker.add_argument("--image-digest", required=True)
    shadow_worker.add_argument("--activation-id", required=True)
    downgrade_preflight = subparsers.add_parser(
        "downgrade-preflight",
        help="read-only check that no OVERNIGHT rows block a binary downgrade",
    )
    downgrade_preflight.add_argument("--database-path", required=True)
    telemetry_export = subparsers.add_parser(
        "shadow-telemetry-export", help="read-only bounded shadow telemetry export"
    )
    telemetry_export.add_argument("--database-path", required=True)
    telemetry_export.add_argument("--activation-id", required=True)
    telemetry_export.add_argument("--session-date-kst", required=True)
    telemetry_export.add_argument("--offset", type=int, default=0)
    telemetry_export.add_argument("--length", type=int, default=12288)
    telemetry_export.add_argument("--manifest-only", action="store_true")
    telemetry_export.add_argument("--row-hashes-only", action="store_true")
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
    if args.command == "downgrade-preflight":
        from kiwoom_stock.core.database import (
            OvernightDowngradePreflightEvidence,
            TradeLogger,
        )

        try:
            evidence = TradeLogger.inspect_overnight_downgrade(
                Path(args.database_path)
            )
        except Exception:
            evidence = OvernightDowngradePreflightEvidence(
                "FAILED",
                None,
                None,
                "INTERNAL_ERROR",
            )
        print(
            json.dumps(
                evidence.to_safe_dict(),
                sort_keys=True,
                separators=(",", ":"),
            ),
            flush=True,
        )
        return evidence.exit_code
    if args.command == "shadow-telemetry-export":
        from base64 import b64encode
        from kiwoom_stock.infrastructure.shadow_telemetry import (
            MAX_PAGE_BYTES, ShadowTelemetryReader,
        )
        try:
            if args.offset < 0 or args.length < 1 or args.length > MAX_PAGE_BYTES:
                raise ValueError("telemetry page bounds are invalid")
            store = ShadowTelemetryReader(args.database_path)
            artifact, manifest = store.export(args.activation_id, args.session_date_kst)
            rows = store.rows(args.activation_id, args.session_date_kst)
            if args.row_hashes_only:
                payload = {"activation_id": args.activation_id, "session_date_kst": args.session_date_kst, "row_hashes": [row["row_sha256"] for row in rows]}
            elif args.manifest_only:
                payload = manifest
            else:
                if args.offset >= len(artifact) or args.offset + args.length > len(artifact):
                    raise ValueError("telemetry page is outside the exported artifact")
                page = artifact[args.offset : args.offset + args.length]
                payload = {
                    **manifest,
                    "offset": args.offset,
                    "length": len(page),
                    "page_sha256": hashlib.sha256(page).hexdigest(),
                    "page_base64": b64encode(page).decode("ascii"),
                }
            print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
            store.close()
            return 0
        except Exception as exc:
            print(json.dumps({"status": "FAILED", "error_type": type(exc).__name__}, separators=(",", ":")))
            return 1
    if args.command in ("shadow-once", "shadow-worker"):
        from kiwoom_stock.application.execution import (
            ActivationTuple,
            ExecutionMode,
            ExecutionPolicy,
            ExecutionPolicyError,
            SHADOW_PROCESS_LOCK_PATH,
        )
        from kiwoom_stock.application.runtime import create_shadow_runtime
        from kiwoom_stock.application.shadow_worker import (
            run_shadow_continuous,
            run_shadow_once_managed,
        )
        from kiwoom_stock.core import config
        from kiwoom_stock.infrastructure.shadow_process_lock import ShadowProcessLock
        from kiwoom_stock.infrastructure.shadow_telemetry import (
            ShadowTelemetryStore,
            shadow_config_sha256,
        )
        from kiwoom_stock.settings import SettingsValidationError

        try:
            settings = config.validate_environment_settings()
            requested_mode = (
                ExecutionMode.SHADOW_ONCE
                if args.command == "shadow-once"
                else ExecutionMode.SHADOW_CONTINUOUS
            )
            if settings.execution.mode is not requested_mode:
                raise ExecutionPolicyError(
                    "CLI command and KIWOOM_EXECUTION_MODE must select the same shadow mode"
            )
            candidate_settings = getattr(settings, "swing_candidate", None)
            candidate_enabled = bool(getattr(candidate_settings, "enabled", False))
            if candidate_settings is None or not candidate_enabled:
                candidate_database_path = None
                candidate_portfolio_id = None
            else:
                candidate_database_path = candidate_settings.database_path
                candidate_portfolio_id = candidate_settings.portfolio_id
            policy = ExecutionPolicy.for_request(
                settings.execution.mode,
                ActivationTuple(
                    source_sha=args.source_sha,
                    image_digest=args.image_digest,
                    activation_id=args.activation_id,
                ),
                swing_candidate_enabled=candidate_enabled,
                swing_candidate_database_path=candidate_database_path,
                swing_candidate_portfolio_id=candidate_portfolio_id,
            )
            runtime_factory = lambda admitted, admission: create_shadow_runtime(
                policy=admitted,
                settings=settings,
                admission=admission,
            )
            telemetry_path = os.environ.get("KIWOOM_SHADOW_TELEMETRY_PATH")
            if os.environ.get("KIWOOM_REQUIRE_SHADOW_TELEMETRY") == "1" and not telemetry_path:
                raise RuntimeError("shadow telemetry sidecar path is required")
            telemetry_store = (
                ShadowTelemetryStore(telemetry_path)
                if telemetry_path
                else None
            )
            config_sha256 = shadow_config_sha256(settings) if telemetry_store is not None else None
            telemetry_commit = (
                (lambda admitted, session_date, receipt: telemetry_store.commit_receipt(
                    admitted, session_date, receipt, config_sha256=config_sha256
                ))
                if telemetry_store is not None else None
            )
            if requested_mode is ExecutionMode.SHADOW_ONCE:
                once_result = run_shadow_once_managed(
                    policy,
                    lock_path=SHADOW_PROCESS_LOCK_PATH,
                    runtime_factory=runtime_factory,
                    lock_factory=ShadowProcessLock,
                    telemetry_commit=telemetry_commit,
                )
                result_payload = once_result.to_safe_dict()
                exit_code = 0
            else:
                continuous_result = run_shadow_continuous(
                    policy,
                    lock_path=SHADOW_PROCESS_LOCK_PATH,
                    runtime_factory=runtime_factory,
                    emit=lambda evidence: print(
                        json.dumps(evidence, sort_keys=True), flush=True
                    ),
                    lock_factory=ShadowProcessLock,
                    telemetry_commit=telemetry_commit,
                )
                result_payload = continuous_result.to_safe_dict()
                exit_code = continuous_result.exit_code
            if telemetry_store is not None and exit_code == 0:
                from datetime import datetime, timezone
                from zoneinfo import ZoneInfo
                telemetry_store.finalize_session(
                    policy.activation.activation_id,
                    datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Seoul")).date().isoformat(),
                )
        except SettingsValidationError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        except BaseException as exc:
            failure = json.dumps(
                {"status": "FAILED", "error_type": type(exc).__name__},
                sort_keys=True,
            )
            # Keep the bounded type-only sentinel visible to container log
            # collectors; no exception message or credential-bearing payload
            # is ever emitted.
            print(failure, file=sys.stderr)
            print(failure, flush=True)
            return 1
        if telemetry_store is not None:
            telemetry_store.close()
        print(json.dumps(result_payload, sort_keys=True))
        return exit_code
    parser.print_help()
    return 0
