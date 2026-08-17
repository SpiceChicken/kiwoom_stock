"""External settings input sources isolated from settings validation."""

import importlib.resources as resources
import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Optional

from kiwoom_stock.settings_contracts import SettingsIssue, SettingsValidationError


@dataclass(frozen=True)
class EnvironmentSnapshot:
    """Immutable process-environment snapshot captured at one boundary."""

    values: Mapping[str, str]

    @classmethod
    def capture(cls, environment: Mapping[str, str]) -> "EnvironmentSnapshot":
        return cls(MappingProxyType(dict(environment)))


@dataclass(frozen=True)
class LegacyDocumentSet:
    """Raw, validated JSON documents before compatibility projection."""

    config: Optional[Mapping[str, Any]] = None
    strategy_config: Optional[Mapping[str, Any]] = None
    scoring_config: Optional[Mapping[str, Any]] = None
    notices: tuple[str, ...] = ()


def _legacy_package_root(package_name: str) -> Any:
    try:
        return resources.files(package_name)
    except ModuleNotFoundError:
        return None
    except TypeError as exc:
        raise SettingsValidationError(
            (SettingsIssue("LEGACY_CONFIG_PACKAGE", "must be an importable resource package"),)
        ) from exc


def _read_legacy_resource(resource: Any, issues: list[SettingsIssue]) -> Optional[Mapping[str, Any]]:
    try:
        parsed = json.loads(resource.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        issues.append(SettingsIssue("LEGACY.%s" % resource.name, "must contain a readable JSON object"))
        return None
    if not isinstance(parsed, Mapping):
        issues.append(SettingsIssue("LEGACY.%s" % resource.name, "must contain a JSON object"))
        return None
    return parsed


def load_legacy_documents(package_name: str = "config") -> LegacyDocumentSet:
    """Read supported legacy JSON resources without applying settings precedence."""

    package_root = _legacy_package_root(package_name)
    if package_root is None:
        return LegacyDocumentSet()
    file_targets = {
        "config.json": "config",
        "strategy_config.json": "strategy_config",
        "scoring_config.json": "scoring_config",
    }
    loaded: dict[str, Mapping[str, Any]] = {}
    notices: list[str] = []
    issues: list[SettingsIssue] = []
    for resource in sorted(package_root.iterdir(), key=lambda item: item.name):
        if not resource.name.endswith(".json"):
            continue
        target = file_targets.get(resource.name)
        if target is None:
            notices.append(
                "Unknown legacy JSON resource %s was ignored; migrate or remove it." % resource.name
            )
            continue
        parsed = _read_legacy_resource(resource, issues)
        if parsed is not None:
            loaded[target] = parsed
    if issues:
        raise SettingsValidationError(issues)
    return LegacyDocumentSet(
        loaded.get("config"),
        loaded.get("strategy_config"),
        loaded.get("scoring_config"),
        tuple(notices),
    )
