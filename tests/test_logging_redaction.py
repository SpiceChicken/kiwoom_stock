import io
import logging
from unittest.mock import Mock

import requests

from kiwoom_stock.api.auth import Authenticator
from kiwoom_stock.application.credentials import (
    KiwoomClientCredentials,
    SensitiveText,
)
from kiwoom_stock.settings import KiwoomEndpoint
from kiwoom_stock.utils import (
    SecretRedactionFilter,
    register_sensitive_values,
    setup_preflight_logging,
    setup_structured_logging,
    unregister_sensitive_values,
)


def _capture_logger():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(SecretRedactionFilter())
    handler.setFormatter(logging.Formatter("%(message)s %(exc_text)s"))
    logger = logging.getLogger("test.secret-redaction")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    return logger, handler, stream


def test_redaction_filter_covers_nested_message_args_bearer_and_exception():
    app_key = "sentinel-app-key"
    secret_key = "sentinel-secret-key"
    token = "sentinel-token"
    register_sensitive_values(app_key, secret_key, token)
    logger, handler, stream = _capture_logger()
    try:
        logger.info(
            {"nested": [{"app": app_key}], "authorization": f"Bearer {token}"}
        )
        logger.info("secret=%s token=%s", secret_key, token)
        try:
            raise RuntimeError(f"nested exception {secret_key} Bearer {token}")
        except RuntimeError:
            logger.exception("request failed")
        handler.flush()
        output = stream.getvalue()
    finally:
        unregister_sensitive_values(app_key, secret_key, token)
        logger.handlers = []
        handler.close()

    assert app_key not in output
    assert secret_key not in output
    assert token not in output
    assert output.count("[REDACTED]") >= 4


def test_bearer_delimiter_suffixes_are_fully_redacted_in_all_record_shapes():
    tokens = (
        "part-a,part-b",
        "part-a;part-b",
        'part-a"part-b',
        "part-a'part-b",
    )
    register_sensitive_values(*tokens)
    logger, handler, stream = _capture_logger()
    try:
        for token in tokens:
            logger.info({"nested": [{"authorization": f"Bearer {token}"}]})
            logger.info("authorization=%s", f"Bearer {token}")
            try:
                raise ValueError(f"Bearer {token}")
            except ValueError:
                logger.exception("delimiter failure")
        handler.flush()
        output = stream.getvalue()
    finally:
        unregister_sensitive_values(*tokens)
        logger.handlers = []
        handler.close()

    for token in tokens:
        assert token not in output
        assert token.split(token[6] if len(token) > 6 else ",")[-1] not in output
    assert output.count("Bearer [REDACTED]") >= len(tokens) * 3


def test_exception_redaction_preserves_type_frame_and_original_exception():
    secret = "TRACEBACK-SENTINEL-987654"
    register_sensitive_values(secret)
    logger, handler, stream = _capture_logger()
    error = ValueError(f"diagnostic {secret}")

    def diagnostic_frame():
        raise error

    try:
        try:
            diagnostic_frame()
        except ValueError:
            logger.exception("typed failure")
        handler.flush()
        output = stream.getvalue()
    finally:
        unregister_sensitive_values(secret)
        logger.handlers = []
        handler.close()

    assert error.args == (f"diagnostic {secret}",)
    assert secret not in output
    assert "ValueError: diagnostic [REDACTED]" in output
    assert "diagnostic_frame" in output


def test_structured_logging_early_return_repairs_all_marked_handlers():
    root = logging.getLogger()
    status = logging.getLogger("status")
    root_handler = logging.StreamHandler(io.StringIO())
    status_handler = logging.StreamHandler(io.StringIO())
    setattr(root_handler, "_kiwoom_structured_file", True)
    setattr(status_handler, "_kiwoom_structured_file", True)
    root.addHandler(root_handler)
    status.addHandler(status_handler)
    try:
        setup_structured_logging()
        assert any(
            isinstance(item, SecretRedactionFilter)
            for item in root_handler.filters
        )
        assert any(
            isinstance(item, SecretRedactionFilter)
            for item in status_handler.filters
        )
    finally:
        root.removeHandler(root_handler)
        status.removeHandler(status_handler)
        root_handler.close()
        status_handler.close()


def test_every_handler_configured_by_setup_has_redaction_filter(monkeypatch, tmp_path):
    root = logging.getLogger()
    status = logging.getLogger("status")
    root_before = tuple(root.handlers)
    status_before = tuple(status.handlers)
    monkeypatch.chdir(tmp_path)
    try:
        setup_preflight_logging()
        setup_structured_logging()
        configured = [
            handler
            for target in (root, status)
            for handler in target.handlers
            if (
                getattr(handler, "_kiwoom_preflight_console", False)
                or getattr(handler, "_kiwoom_structured_file", False)
            )
        ]
        assert configured
        assert all(
            any(isinstance(item, SecretRedactionFilter) for item in handler.filters)
            for handler in configured
        )
    finally:
        for target, previous in ((root, root_before), (status, status_before)):
            for handler in tuple(target.handlers):
                if handler not in previous:
                    target.removeHandler(handler)
                    handler.close()


def test_authenticator_close_releases_registered_sentinels_without_network():
    app_key = "LIFECYCLECLOSE987654"
    secret_key = "".join(("LIFECYCLE", "SECRET", "CLOSE", "987654"))
    session = Mock(spec=requests.Session)
    auth = Authenticator(
        KiwoomClientCredentials(
            SensitiveText(app_key),
            SensitiveText(secret_key),
        ),
        KiwoomEndpoint.MOCK,
        session=session,
    )
    logger, handler, stream = _capture_logger()
    try:
        logger.info("before=%s", app_key)
        auth.close()
        logger.info("after=%s", app_key)
        handler.flush()
        output = stream.getvalue().splitlines()
    finally:
        logger.handlers = []
        handler.close()

    assert app_key not in output[0]
    assert app_key in output[1]
    session.post.assert_not_called()
