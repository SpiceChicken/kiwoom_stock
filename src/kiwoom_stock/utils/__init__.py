import os
import re
import sys
import logging
import traceback
from logging.handlers import TimedRotatingFileHandler
from threading import RLock
from typing import Any

# --- [로깅 시스템 고도화 설정] ---

_PREFLIGHT_HANDLER_MARKER = "_kiwoom_preflight_console"
_STRUCTURED_HANDLER_MARKER = "_kiwoom_structured_file"
_REDACTION_FILTER_MARKER = "_kiwoom_secret_redaction"
_REDACTED = "[REDACTED]"
_BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+\S+")
_sensitive_values: dict[str, int] = {}
_sensitive_values_lock = RLock()


def register_sensitive_values(*values: str) -> None:
    """Register exact in-process sentinels for handler-level defense.

    Callers must still log allowlisted fields only. This registry is a second
    layer and intentionally makes no claim of universal redaction.
    """

    with _sensitive_values_lock:
        for value in values:
            if value:
                _sensitive_values[value] = _sensitive_values.get(value, 0) + 1


def unregister_sensitive_values(*values: str) -> None:
    """Release registry references when the owning secret lifecycle closes."""

    with _sensitive_values_lock:
        for value in values:
            count = _sensitive_values.get(value, 0)
            if count <= 1:
                _sensitive_values.pop(value, None)
            else:
                _sensitive_values[value] = count - 1


def _redact_text(value: str) -> str:
    with _sensitive_values_lock:
        sentinels = tuple(sorted(_sensitive_values, key=len, reverse=True))
    redacted = value
    for sentinel in sentinels:
        redacted = redacted.replace(sentinel, _REDACTED)
    return _BEARER_PATTERN.sub(f"Bearer {_REDACTED}", redacted)


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, dict):
        return {
            _redact_value(key): _redact_value(item)
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return tuple(_redact_value(item) for item in value)
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, set):
        return {_redact_value(item) for item in value}
    if isinstance(value, BaseException):
        return _redact_text(str(value))
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact_text(str(value))


class SecretRedactionFilter(logging.Filter):
    """Redact registered values and Bearer credentials before formatting."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _redact_value(record.msg)
        record.args = _redact_value(record.args)
        if record.exc_info:
            record.exc_text = _redact_text(
                "".join(traceback.format_exception(*record.exc_info))
            )
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = _redact_text(record.exc_text)
        return True


class ExcludeErrorFilter(logging.Filter):
    """ERROR(40) 레벨 이상의 로그를 제외하여 trading.log를 깨끗하게 유지"""
    def filter(self, record):
        return record.levelno < logging.ERROR


def _standard_formatter():
    return logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', '%H:%M:%S')


def _json_formatter():
    return logging.Formatter(
        '{"timestamp": "%(asctime)s", "level": "%(levelname)s", "module": "%(module)s", "message": "%(message)s"}'
    )


def _has_marked_handler(logger, marker):
    return any(getattr(handler, marker, False) for handler in logger.handlers)


def _attach_redaction_filter(handler):
    if not getattr(handler, _REDACTION_FILTER_MARKER, False):
        handler.addFilter(SecretRedactionFilter())
        setattr(handler, _REDACTION_FILTER_MARKER, True)


def setup_preflight_logging():
    """초기 guard와 설정 오류를 콘솔에만 기록합니다."""
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    if _has_marked_handler(root_logger, _PREFLIGHT_HANDLER_MARKER):
        for handler in root_logger.handlers:
            if getattr(handler, _PREFLIGHT_HANDLER_MARKER, False):
                _attach_redaction_filter(handler)
        return

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(_standard_formatter())
    _attach_redaction_filter(console_handler)
    setattr(console_handler, _PREFLIGHT_HANDLER_MARKER, True)
    root_logger.addHandler(console_handler)


def setup_structured_logging():
    """Trading, Error, Status 로그를 분리하여 초기화합니다."""
    setup_preflight_logging()
    log_dir = "logs"
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)

    json_format = _json_formatter()
    root_logger = logging.getLogger()
    status_logger = logging.getLogger("status")

    for target_logger in (root_logger, status_logger):
        for handler in target_logger.handlers:
            if getattr(handler, _STRUCTURED_HANDLER_MARKER, False):
                _attach_redaction_filter(handler)

    if (
        _has_marked_handler(root_logger, _STRUCTURED_HANDLER_MARKER)
        and _has_marked_handler(status_logger, _STRUCTURED_HANDLER_MARKER)
    ):
        return

    # Trading Log 핸들러: 일반 운영 로그 (INFO~WARNING, 에러 제외)
    if not _has_marked_handler(root_logger, _STRUCTURED_HANDLER_MARKER):
        trading_handler = TimedRotatingFileHandler(
            filename=f"{log_dir}/trading.log", when="midnight", interval=1, backupCount=30, encoding="utf-8"
        )
        trading_handler.setLevel(logging.INFO)
        trading_handler.setFormatter(json_format)
        trading_handler.addFilter(ExcludeErrorFilter())  # 에러는 여기서 제외
        _attach_redaction_filter(trading_handler)
        setattr(trading_handler, _STRUCTURED_HANDLER_MARKER, True)
        root_logger.addHandler(trading_handler)

        # Error Log 핸들러: 장애 로그 (ERROR~CRITICAL만 기록)
        error_handler = TimedRotatingFileHandler(
            filename=f"{log_dir}/error.log", when="midnight", interval=1, backupCount=90, encoding="utf-8"
        )
        error_handler.setLevel(logging.ERROR)
        error_handler.setFormatter(json_format)
        _attach_redaction_filter(error_handler)
        setattr(error_handler, _STRUCTURED_HANDLER_MARKER, True)
        root_logger.addHandler(error_handler)

    # [2] Status Logger 설정 (50개 종목 상태 전용)
    # propagate=False를 설정하여 trading.log에 중복 기록되는 것을 방지합니다.
    status_logger.setLevel(logging.INFO)
    status_logger.propagate = False

    if not _has_marked_handler(status_logger, _STRUCTURED_HANDLER_MARKER):
        status_handler = TimedRotatingFileHandler(
            filename=f"{log_dir}/status.log", when="midnight", interval=1, backupCount=7, encoding="utf-8"
        )
        # 상태 데이터는 표 형태이므로 날짜만 붙은 심플한 포맷 사용
        status_handler.setFormatter(logging.Formatter('%(message)s'))
        _attach_redaction_filter(status_handler)
        setattr(status_handler, _STRUCTURED_HANDLER_MARKER, True)
        status_logger.addHandler(status_handler)
