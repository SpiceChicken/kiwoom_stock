"""No-side-effect package command entry points."""

import argparse
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
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
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
    parser.print_help()
    return 0
